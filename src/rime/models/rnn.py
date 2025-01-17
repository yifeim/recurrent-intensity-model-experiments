import numpy as np
import functools, warnings

import torch
from torch.utils.data import DataLoader
from torch.nn.utils.rnn import pack_sequence, pad_packed_sequence

from .third_party.word_language_model import RNNModel

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import LearningRateMonitor
from ..util import (_LitValidated, empty_cache_on_exit, _ReduceLRLoadCkpt,
                    default_random_split, LazyDenseMatrix, matrix_reindex)


class RNN:
    def __init__(
        self, item_df, max_item_size=int(30e3),
        num_hidden=128, nlayers=2, max_epochs=20, gpus=int(torch.cuda.is_available()),
        truncated_input_steps=256, truncated_bptt_steps=32, batch_size=64,
        load_from_checkpoint=None, auto_pad_item=True,
    ):
        self._padded_item_list = [None] * auto_pad_item + item_df.index[:max_item_size].tolist()
        self._tokenize = {k: i for i, k in enumerate(self._padded_item_list)}
        self._truncated_input_steps = truncated_input_steps

        self.model = _LitRNNModel(
            'GRU', len(self._padded_item_list),
            num_hidden, num_hidden, nlayers, dropout=0, tie_weights=True,
            truncated_bptt_steps=truncated_bptt_steps)

        if load_from_checkpoint is not None:
            self.model.load_state_dict(
                torch.load(load_from_checkpoint)['state_dict'])

        self.trainer = Trainer(
            max_epochs=max_epochs, gpus=gpus,
            callbacks=[self.model._checkpoint, LearningRateMonitor()])
        print("trainer log at:", self.trainer.logger.log_dir)
        self.batch_size = batch_size

    def _extract_data(self, user_df):
        return user_df['_hist_items'].apply(
            lambda x: [0] + [self._tokenize[i] for i in x if i in self._tokenize]).values

    @empty_cache_on_exit
    @torch.no_grad()
    def transform(self, D):
        dataset = self._extract_data(D.user_in_test)
        collate_fn = functools.partial(
            _collate_fn, truncated_input_steps=self._truncated_input_steps, training=False)
        m, n_events, sample = _get_dataset_stats(dataset, collate_fn)
        print(f"transforming {m} users with {n_events} events, "
              f"truncated@{self._truncated_input_steps} per user")
        print(f"sample[0]={sample[0].tolist()}")
        print(f"sample[1]={sample[1].tolist()}")

        if hasattr(dataset, "tolist"):  # pytorch lightning bug cannot take array input
            dataset = dataset.tolist()
        batches = self.trainer.predict(
            self.model,
            dataloaders=DataLoader(dataset, 1000, collate_fn=collate_fn))
        user_hidden, user_log_bias = [np.concatenate(x) for x in zip(*batches)]

        item_hidden = self.model.model.decoder.weight.detach().cpu().numpy()
        item_log_bias = self.model.model.decoder.bias.detach().cpu().numpy()
        item_reindex = lambda x, fill_value=0: matrix_reindex(
            x, self._padded_item_list, D.item_in_test.index, axis=0, fill_value=fill_value)

        return (LazyDenseMatrix(user_hidden) @ item_reindex(item_hidden).T
                + user_log_bias[:, None] + item_reindex(item_log_bias, -np.inf)[None, :]).exp()

    @empty_cache_on_exit
    def fit(self, D):
        dataset = self._extract_data(D.user_df[D.user_df['_hist_len'] > 0])
        collate_fn = functools.partial(
            _collate_fn, truncated_input_steps=self._truncated_input_steps, training=True)
        m, n_events, sample = _get_dataset_stats(dataset, collate_fn)
        print(f"fitting {m} users with {n_events} events, "
              f"truncated@{self._truncated_input_steps} per user")
        print(f"sample[0]={sample[0].tolist()}")
        print(f"sample[1]={sample[1].tolist()}")

        train_set, valid_set = default_random_split(dataset)
        self.trainer.fit(
            self.model,
            DataLoader(train_set, self.batch_size, collate_fn=collate_fn, shuffle=True),
            DataLoader(valid_set, self.batch_size, collate_fn=collate_fn),)
        self.model._load_best_checkpoint("best")

        for name, param in self.model.named_parameters():
            print(name, param.data.shape)
        return self


def _collate_fn(batch, truncated_input_steps, training):
    if truncated_input_steps > 0:
        batch = [seq[-(truncated_input_steps + training):] for seq in batch]
    batch = [torch.tensor(seq, dtype=torch.int64) for seq in batch]
    batch, lengths = pad_packed_sequence(pack_sequence(batch, enforce_sorted=False))
    if training:
        return (batch[:-1].transpose(0, 1), batch[1:].transpose(0, 1))  # TBPTT assumes NT layout
    else:
        return (batch, lengths)  # RNN default TN layout


def _get_dataset_stats(dataset, collate_fn):
    truncated_input_steps = collate_fn.keywords['truncated_input_steps']
    n_events = sum([min(truncated_input_steps, len(x)) for x in dataset])
    sample = next(iter(DataLoader(dataset, 1, collate_fn=collate_fn, shuffle=True)))
    return len(dataset), n_events, sample


class _LitRNNModel(_LitValidated):
    def __init__(self, *args, truncated_bptt_steps, lr=0.1, **kw):
        super().__init__()
        self.model = RNNModel(*args, **kw)
        self.loss = torch.nn.NLLLoss(ignore_index=0)
        self.truncated_bptt_steps = truncated_bptt_steps
        self.lr = lr

    def forward(self, batch):
        """ output user embedding at lengths-1 positions """
        TN_layout, lengths = batch
        last_hidden, log_bias = self.model.forward_last_prediction(TN_layout, lengths)
        return last_hidden.cpu().numpy(), log_bias.cpu().numpy()

    def configure_optimizers(self):
        optimizer = torch.optim.Adagrad(self.parameters(), eps=1e-3, lr=self.lr)
        lr_scheduler = _ReduceLRLoadCkpt(
            optimizer, model=self, factor=0.25, patience=4, verbose=True)
        return {"optimizer": optimizer, "lr_scheduler": {
                "scheduler": lr_scheduler, "monitor": "val_loss"
                }}

    def training_step(self, batch, batch_idx, hiddens=None):
        """ truncated_bptt_steps pass batch[:][:, slice] and hiddens """
        x, y = batch[0].T, batch[1].T   # transpose to TN layout
        if hiddens is None:
            hiddens = self.model.init_hidden(x.shape[1])
        else:
            hiddens = hiddens.detach()
        out, hiddens = self.model(x, hiddens)
        loss = self.loss(out, y.view(-1))
        self.log("train_loss", loss)
        return {'loss': loss, 'hiddens': hiddens}
