import pandas as pd, numpy as np, scipy as sp
import functools, collections, time, contextlib, torch, gc, warnings
from torch.utils.data import DataLoader, random_split
from pytorch_lightning import LightningModule
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from backports.cached_property import cached_property
from .score_array import *  # noqa: F401, F403
from .plotting import plot_rec_results, plot_mtch_results


class timed(contextlib.ContextDecorator):
    def __init__(self, name="", inline=True):
        self.name = name
        self.inline = inline

    def __enter__(self):
        self.tic = time.time()
        print("timing", self.name, end=' ' if self.inline else '\n')

    def __exit__(self, *args, **kw):
        print("done", "." if self.inline else self.name,
              "time {:.1f}s".format(time.time() - self.tic))


def warn_nan_output(func):
    @functools.wraps(func)
    def wrapped(*args, **kw):
        out = func(*args, **kw)
        values = getattr(out, "values", out)

        if hasattr(values, "has_nan"):
            has_nan = values.has_nan()
        else:
            has_nan = np.isnan(values).any()

        if has_nan:
            warnings.warn(f"{func.__name__} output contains NaN", stacklevel=2)
        return out
    return wrapped


def _empty_cache():
    gc.collect()
    torch.cuda.empty_cache()


def _get_cuda_objs():
    objs = []
    for obj in gc.get_objects():
        try:
            flag = torch.is_tensor(obj)  # or \
            # (hasattr(obj, 'data') and torch.is_tensor(obj.data))
        except Exception:
            flag = False
        if flag and torch.device(obj.device) != torch.device("cpu"):
            objs.append(obj)
    return objs


def empty_cache_on_exit(func):
    @functools.wraps(func)
    def wrapped(*args, **kw):
        _empty_cache()
        start_list = _get_cuda_objs()

        tic = time.time()
        out = func(*args, **kw)
        print(func.__name__, "time {:.1f}s".format(time.time() - tic))

        _empty_cache()
        end_list = _get_cuda_objs()
        for obj in set(end_list) - set(start_list):
            print(func.__name__, "memory leak",
                  type(obj), obj.size(), obj.device, flush=True)

        del start_list
        del end_list
        _empty_cache()
        return out
    return wrapped


@contextlib.contextmanager
def _to_cuda(model):
    if torch.cuda.is_available():
        orig_device = model.device
        print("running model on cuda device")
        yield model.to("cuda")
        model.to(orig_device)
    else:
        yield model


def perplexity(x):
    x = np.ravel(x) / x.sum()
    return float(np.exp(- x @ np.log(np.where(x > 0, x, 1e-10))))


@empty_cache_on_exit
def _assign_topk(S, k, tie_breaker=1e-10, device="cpu"):
    """ Return a sparse matrix where each row contains k non-zero values.

    Used for both ItemRec (if S is user-by-item) and UserRec (if S is transposed).
    Expect the shape to align with (len(D.user_in_test), len(D.item_in_test))
    or its transpose.
    """
    indices = []
    if hasattr(S, "collate_fn"):
        batches = map(lambda i: S[i:min(len(S), i + S.batch_size)],
                      range(0, len(S), S.batch_size))
    else:
        batches = [S]
    for s in batches:
        if hasattr(s, "eval"):
            s = s.eval(device)
        else:
            s = torch.tensor(s, device=device)
        if tie_breaker:
            s = s + torch.rand(*s.shape, device=device) * tie_breaker
        s_topk = s.topk(k).indices.cpu().numpy()
        indices.append(s_topk)
    indices = np.vstack(indices)

    return sp.sparse.csr_matrix((
        np.ones(indices.size),
        np.ravel(indices),
        np.arange(0, indices.size + 1, indices.shape[1]),
    ), shape=S.shape)


assign_topk = _assign_topk


@empty_cache_on_exit
def _argsort(S, tie_breaker=1e-10, device="cpu"):
    print(f"_argsort {S.size:,} scores on device {device}; ", end="")
    if hasattr(S, "batch_size") and S.batch_size < S.shape[0]:
        warnings.warn(f"switching numpy.argsort due to {S.batch_size}<{S.shape[0]}")
        device = None

    if hasattr(S, "eval"):
        S = S.eval(device)

    shape = S.shape

    if device is None:
        if tie_breaker > 0:
            S = S + np.random.rand(*S.shape) * tie_breaker
        S = -S.reshape(-1)
        _empty_cache()
        argsort_ind = np.argsort(S)
    else:
        S = torch.as_tensor(S, device=device)
        if tie_breaker > 0:
            S = S + torch.rand(*S.shape, device=device) * tie_breaker
        S = -S.reshape(-1)
        _empty_cache()
        argsort_ind = torch.argsort(S).cpu().numpy()

    return np.unravel_index(argsort_ind, shape)


def extract_user_item(event_df):
    user_df = event_df.groupby('USER_ID').agg(
        _Tmin=("TIMESTAMP", min), _Tmax=("TIMESTAMP", max)
    )
    item_df = event_df.groupby('ITEM_ID').size().to_frame("_siz")
    return (user_df, item_df)


def groupby_unexplode(series, index=None, return_type='series'):
    """
    assume the input is an exploded dataframe with block-wise indices
    >>> groupby_unexplode(pd.Series([1,2,3,4,5], index=[1,1,2,3,3])).to_dict()
    {1: [1, 2], 2: [3], 3: [4, 5]}
    >>> groupby_unexplode(pd.Series([1,2,3,4,5], index=[1,1,2,3,3]), index=[0,1,-1,2,3,4]).to_dict()
    {0: [], 1: [1, 2], -1: [], 2: [3], 3: [4, 5], 4: []}
    """
    if len(series) == 0:
        return pd.Series(index=index)

    if index is None:
        splits = np.where(
            np.array(series.index.values[1:]) !=  # 1, 2, 3, 3
            np.array(series.index.values[:-1])    # 1, 1, 2, 3
        )[0] + 1  # [2, 3]
        index = series.index.values[np.hstack([[0], splits])]  # 1, 2, 3
    else:  # something like searchsorted, but unordered
        splits = []
        tape = enumerate(series.index)
        N = len(series)
        i, value = next(tape)

        for key in index:
            splits.append(i)
            while i < N and key == value:
                i, value = next(tape, (N, None))  # move past the current chunk
        splits = splits[1:]

    if return_type == 'splits':
        return splits
    else:
        return pd.Series([x.tolist() for x in np.split(series.values, splits)],
                         index=index)


def indices2csr(indices, shape1):
    indptr = np.cumsum([0] + [len(x) for x in indices])
    return sps.csr_matrix((
        np.ones(indptr[-1]), np.hstack(indices), indptr
    ), shape=(len(indices), shape1))


def extract_past_ij(user_df, item_index):
    past_event_df = user_df.reset_index()['_hist_items'].explode().to_frame("ITEM_ID").join(
        pd.Series({k: j for j, k in enumerate(item_index)}).to_frame('j'),
        on="ITEM_ID", how="inner")  # drop empty users and oov items
    return (past_event_df.index.values, past_event_df['j'].values)


def create_matrix(event_df, user_index, item_index, return_type='csr'):
    """ create matrix and prune unknown indices """
    user2ind = {k: i for i, k in enumerate(user_index)}
    item2ind = {k: i for i, k in enumerate(item_index)}
    event_df = event_df[
        event_df['USER_ID'].isin(set(user_index)) &
        event_df['ITEM_ID'].isin(set(item_index))
    ]
    i = [user2ind[k] for k in event_df['USER_ID']]
    j = [item2ind[k] for k in event_df['ITEM_ID']]

    if return_type == 'ij':
        return (i, j)

    data = np.ones(len(event_df))
    shape = (len(user_index), len(item_index))
    csr = sp.sparse.coo_matrix((data, (i, j)), shape=shape).tocsr()

    if return_type == 'csr':
        return csr
    elif return_type == 'df':
        return pd.DataFrame.sparse.from_spmatrix(csr, user_index, item_index)


def fill_factory_inplace(df, isna, kv):
    for k, v in kv.items():
        if k is None:  # series
            df[:] = [v() if do else x for do, x in zip(isna, df.values)]
        elif k in df.columns:
            df[k] = [v() if do else x for do, x in zip(isna, df[k])]
        else:
            warnings.warn(f"fill_factory_inplace missing {k}")


def sample_groupA(user_df, frac=0.5, seed=888):
    return user_df.index.isin(
        user_df.sample(frac=frac, random_state=seed).index
    )


def filter_min_len(event_df, min_user_len, min_item_len):
    """ CAVEAT: use in conjunction with dataclass filter to avoid future-leaking bias """
    users = event_df.groupby('USER_ID').size()
    items = event_df.groupby('ITEM_ID').size()
    return event_df[
        event_df['USER_ID'].isin(users[users >= min_user_len].index) &
        event_df['ITEM_ID'].isin(items[items >= min_item_len].index)
    ]


def get_top_items(item_df, max_item_size, sort_by='_hist_len'):
    sorted_items = item_df.sort_values(sort_by, ascending=False, kind='mergesort')
    if len(sorted_items) > max_item_size:
        warnings.warn(f"clipping item size from {len(sorted_items)} to {max_item_size}")
    return sorted_items.iloc[:max_item_size]


def explode_user_titles(user_hist, item_titles, gamma=0.5, min_gamma=0.1, pad_title='???'):
    """ explode last few user events and match with item titles;
    return splits and discount weights; empty user_hist will be turned into a single pad_title. """

    keep_last = int(np.log(min_gamma) / np.log(np.clip(gamma, 1e-10, 1 - 1e-10))) + 1  # default=4

    explode_titles = pd.Series([x[-keep_last:] for x in user_hist.values]).explode() \
        .to_frame('ITEM_ID').join(item_titles.to_frame('TITLE'), on='ITEM_ID')['TITLE']
    explode_titles = pd.Series(
        [x if not na else pad_title for x, na in
         zip(explode_titles.tolist(), explode_titles.isna().tolist())],
        index=explode_titles.index)

    splits = np.where(
        np.array(explode_titles.index.values[1:]) != np.array(explode_titles.index.values[:-1])
    )[0] + 1

    weights = np.hstack([gamma ** (np.cumsum(x) - np.sum(x))  # -2, -1, 0
                        for x in np.split(np.ones(len(explode_titles)), splits)])
    weights = np.hstack([x / x.sum() for x in np.split(weights, splits)])

    return explode_titles.values, splits, weights


class _LitValidated(LightningModule):
    def validation_step(self, batch, batch_idx):
        loss = self.training_step(batch, batch_idx)
        if isinstance(loss, collections.abc.Mapping) and 'loss' in loss:
            loss = loss['loss']
        self.log("val_batch_loss", loss)
        return loss

    def validation_epoch_end(self, outputs):
        val_epoch_loss = torch.stack(outputs).mean()
        self.log("val_epoch_loss", val_epoch_loss, prog_bar=True)
        self.val_epoch_loss = val_epoch_loss

    @cached_property
    def _checkpoint(self):
        return ModelCheckpoint(monitor="val_epoch_loss", save_weights_only=True)

    def _load_best_checkpoint(self, msg="loading"):
        best_model_path = self._checkpoint.best_model_path
        best_model_score = self._checkpoint.best_model_score
        if best_model_score is not None:
            print(f"{msg} checkpoint {best_model_path} with score {best_model_score}")
            self.load_state_dict(torch.load(best_model_path)['state_dict'])


class _ReduceLRLoadCkpt(torch.optim.lr_scheduler.ReduceLROnPlateau):
    def __init__(self, *args, model, **kw):
        super().__init__(*args, **kw)
        self.model = model

    def _reduce_lr(self, epoch):
        super()._reduce_lr(epoch)
        self.model._load_best_checkpoint()


def default_random_split(dataset):
    N = len(dataset)
    if N >= 5:
        return random_split(dataset, [N * 4 // 5, N - N * 4 // 5])
    else:
        warnings.warn(f"short dataset len={len(dataset)}; "
                      "setting valid_set identical to train_set")
        return dataset, dataset
