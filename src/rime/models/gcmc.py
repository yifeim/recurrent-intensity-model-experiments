import torch, argparse, numpy as np, warnings
from torch.utils.data import DataLoader, random_split
from torch.distributions.categorical import Categorical
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import LearningRateMonitor
from ..util import (_LitValidated, _ReduceLRLoadCkpt,
    empty_cache_on_exit, create_matrix, create_second_order_dataframe)
from .bpr import BPR, _BPR
try:
    import dgl, dgl.function as fn
except ImportError:
    warnings.warn("GCMC requires dgl package")


class _GCMC(_BPR, _LitValidated):
    """ module to compute user RFM embedding. change default lr=0.1 """
    def __init__(self, user_proposal, item_proposal, no_components=32,
        n_negatives=10, lr=0.1, weight_decay=1e-5,
        recency_boundaries=[0.1, 0.3, 1, 3, 10]):
        super(_LitValidated, self).__init__()
        self.register_buffer("user_proposal", torch.as_tensor(user_proposal))
        self.register_buffer("item_proposal", torch.as_tensor(item_proposal))
        self.register_buffer("recency_boundaries", torch.as_tensor(recency_boundaries))

        self.item_encoder = torch.nn.Embedding(len(item_proposal), no_components)
        self.item_bias_vec = torch.nn.Embedding(len(item_proposal), 1)
        self.conv = dgl.nn.pytorch.conv.GraphConv(no_components, no_components, "none")
        self.recency_encoder = torch.nn.Embedding(len(recency_boundaries)+1, 1)
        self.log_sigmoid = torch.nn.LogSigmoid()

        self.user_rec = True
        self.item_rec = True
        self.n_negatives = n_negatives
        self.lr = lr
        self.weight_decay = weight_decay

        self.init_weights()

    def init_weights(self):
        initrange = 0.1
        torch.nn.init.uniform_(self.item_encoder.weight, -initrange, initrange)
        torch.nn.init.zeros_(self.item_bias_vec.weight)
        torch.nn.init.uniform_(self.conv.weight, -initrange, initrange)
        torch.nn.init.zeros_(self.conv.bias)

    def user_encoder(self, i, G=None):
        if G is None:
            G = self.G
        G = G.to(i.device)

        out = self.conv(G, self.item_encoder.weight)
        return out[i]

    def user_bias_vec(self, i, G=None):
        if G is None:
            G = self.G
        G = G.to(i.device)

        G.update_all(lambda x: None, fn.max('t', 'last_t'))
        user_recency = G.nodes['user'].data['test_t'] - G.nodes['user'].data['last_t']

        recency_buckets = torch.bucketize(user_recency, self.recency_boundaries)
        return self.recency_encoder(recency_buckets)[i]


class GCMC:
    def __init__(self, item_df, batch_size=10000, max_epochs=50, **kw):
        """ item_df = D.training_data.item_df """
        self._padded_item_list = [None] + item_df.index.tolist()
        self.batch_size = batch_size
        self.max_epochs = max_epochs
        self._model_kw = kw.copy()

    def _extract_labels(self, V):
        V = V.reindex(self._padded_item_list, axis=1)
        target_coo = V.target_csr.tocoo()
        i, j = target_coo.row, target_coo.col

        user_proposal = (np.ravel(target_coo.sum(axis=1)) + 0.1) ** 0.5
        item_proposal = (np.ravel(target_coo.sum(axis=0)) + 0.1) ** 0.5
        return (i, j), user_proposal, item_proposal

    def _extract_features(self, D):
        """ create item -> user graph """
        D = D.reindex(self._padded_item_list, axis=1)
        event_df = D.training_data.event_df[
            D.training_data.event_df['USER_ID'].isin(set(D.user_in_test.index)) &
            D.training_data.event_df['ITEM_ID'].isin(set(D.item_in_test.index))
        ]
        i, j = create_matrix(event_df, D.user_in_test.index, D.item_in_test.index, 'ij')
        t = event_df['TIMESTAMP'].values / D.horizon

        i = np.hstack([i, np.arange(len(D.user_in_test.index))])
        j = np.hstack([j, np.zeros_like(j, shape=len(D.user_in_test.index))])
        t = np.hstack([t, -np.inf * np.ones_like(t, shape=len(D.user_in_test.index))])

        G = dgl.heterograph(
            {('user','source','item'): (i, j)},
            {'user': len(D.user_in_test.index), 'item': len(D.item_in_test.index)}
        ).reverse() # item -> user
        G.edata['t'] = torch.as_tensor(t)

        user_test_time = D.user_in_test['_timestamps'].apply(lambda x: x[-1]).values
        G.nodes['user'].data['test_t'] = torch.as_tensor(user_test_time / D.horizon)
        return G, D.user_in_test.index

    @empty_cache_on_exit
    def fit(self, V):
        ij_target, user_proposal, item_proposal = self._extract_labels(V)
        dataset = np.array(ij_target, dtype=int).T

        N = len(dataset)
        if len(dataset) > 5:
            train_set, valid_set = random_split(dataset, [N*4//5, (N - N*4//5)])
        else:
            train_set = valid_set = dataset

        model = _GCMC(user_proposal, item_proposal, **self._model_kw)
        trainer = Trainer(max_epochs=self.max_epochs, gpus=int(torch.cuda.is_available()),
            log_every_n_steps=1, callbacks=[model._checkpoint, LearningRateMonitor()])

        G, _ = self._extract_features(V)
        model.G = G
        trainer.fit(model,
            DataLoader(train_set, self.batch_size, shuffle=True, num_workers=(N>1e4)*4),
            DataLoader(valid_set, self.batch_size, num_workers=(N>1e4)*4))
        delattr(model, "G")

        best_model_path = model._checkpoint.best_model_path
        best_model_score = model._checkpoint.best_model_score
        if best_model_score is not None:
            print(f"done fit; best checkpoint {best_model_path} with score {best_model_score}")

        self.item_index = self._padded_item_list
        self.item_embeddings = model.item_encoder.weight.detach().cpu().numpy()
        self.item_biases = model.item_bias_vec.weight.detach().cpu().numpy().ravel()
        self.model = model
        return self

    def transform(self, D):
        G, user_index = self._extract_features(D)
        i = torch.arange(G.num_nodes('user'))
        user_embeddings = self.model.user_encoder(i, G).detach().cpu().numpy()
        user_biases = self.model.user_bias_vec(i, G).detach().cpu().numpy().ravel()

        S = create_second_order_dataframe(
            user_embeddings, self.item_embeddings, user_biases, self.item_biases,
            user_index, self._padded_item_list, 'sigmoid')

        return S.reindex(D.item_in_test.index, axis=1)
