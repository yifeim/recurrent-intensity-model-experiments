import pandas as pd, numpy as np
import torch
from torch.utils.data import DataLoader
from pytorch_lightning import LightningModule, Trainer, loggers
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from ..util import empty_cache_on_exit, get_batch_size, score_op, auto_cast_lazy_score
from ..util.cvx_bisect import dual_solve_u_list, dual_clip, primal_solution


class CVX:
    def __init__(self, score_mat, alpha_lb=-1, alpha_ub=2, beta_lb=-1, beta_ub=2, device='cpu',
                 max_epochs=100, min_epsilon=1e-10, gpus=int(torch.cuda.is_available()),
                 prefix='CVX', return_lazy=False):

        score_mat = auto_cast_lazy_score(score_mat)
        self.score_max = float(score_op(score_mat, "max", device))
        self.score_min = float(score_op(score_mat, "min", device))
        print(f"entering {prefix} CVX score (min={self.score_min}, max={self.score_max})")

        self.model = _LitCVX(
            *score_mat.shape, alpha_lb, alpha_ub, beta_lb, beta_ub, 0.1 / max(score_mat.shape),
            max_epochs, min_epsilon, return_lazy=return_lazy)

        tb_logger = loggers.TensorBoardLogger(
            "logs/",
            name=f"{prefix}-{np.mean(alpha_lb):.3f}-{np.mean(alpha_ub):.3f}"
                 f"-{np.mean(beta_lb):.3f}-{np.mean(beta_ub):.3f}")
        self.trainer = Trainer(max_epochs=max_epochs, gpus=gpus, logger=tb_logger,
                               log_every_n_steps=1, callbacks=[ModelCheckpoint()],
                               # change default save path from . to logger path
                               )
        print("trainer log at:", self.trainer.logger.log_dir)

    @empty_cache_on_exit
    @torch.no_grad()
    def transform(self, score_mat):
        score_mat = auto_cast_lazy_score(score_mat) / self.score_max
        batches = self.trainer.predict(
            self.model,
            DataLoader(score_mat, self.model.batch_size, collate_fn=score_mat[0].collate_fn)
        )
        if self.model.return_lazy:
            u = np.hstack(batches)
            return ((score_mat - u[:, None] - self.model.v) / self.model.epsilon).sigmoid()
        else:
            return np.vstack(batches)  # pi

    @empty_cache_on_exit
    def fit(self, score_mat):
        score_mat = auto_cast_lazy_score(score_mat) / self.score_max
        self.trainer.fit(
            self.model,
            DataLoader(score_mat, self.model.batch_size, True, collate_fn=score_mat[0].collate_fn)
        )
        v = self.model.v.detach().cpu().numpy()
        print('v', pd.Series(v.ravel()).describe().to_dict())
        return self


class _LitCVX(LightningModule):
    def __init__(self, n_users, n_items, alpha_lb, alpha_ub, beta_lb, beta_ub, gtol,
                 max_epochs=100, min_epsilon=1e-10, v=None, epsilon=1, return_lazy=False):

        super().__init__()
        self.alpha_lb = alpha_lb
        self.alpha_ub = alpha_ub
        self.beta_lb = beta_lb
        self.beta_ub = beta_ub
        self.gtol = gtol

        self.batch_size = get_batch_size((n_users, n_items))
        n_batches = n_users / self.batch_size
        self.lr = n_items / n_batches

        self.epsilon = epsilon
        self.epsilon_gamma = (min_epsilon / epsilon) ** (1 / max_epochs)
        self.return_lazy = return_lazy

        if v is None:
            if beta_lb <= 0:  # ub-only
                v = torch.rand(n_items)
            elif beta_ub >= 1:  # lb-only
                v = -torch.rand(n_items)
            else:  # range or eq
                v = torch.rand(n_items) * 2 - 1
        self.v = torch.nn.Parameter(v)

    def configure_optimizers(self):
        return torch.optim.SGD(self.parameters(), lr=self.lr)

    def on_train_epoch_start(self):
        self.epsilon *= self.epsilon_gamma
        self.log("epsilon", self.epsilon, prog_bar=True)

    @torch.no_grad()
    def _solve_u(self, batch):
        (u_neg, u_neg_iters), (u_pos, u_pos_iters) = dual_solve_u_list(
            batch - self.v, [self.alpha_lb, self.alpha_ub], self.epsilon,
            gtol=self.gtol, s_guess=-self.v.max())
        u = u_neg.clip(None, 0) + u_pos.clip(0, None)
        return u, u_neg_iters, u_pos_iters

    @torch.no_grad()
    def _solve_v(self, batch, u):
        (v_neg, v_neg_iters), (v_pos, v_pos_iters) = dual_solve_u_list(
            batch.T - u, [self.beta_lb, self.beta_ub], self.epsilon,
            gtol=self.gtol, s_guess=-u.max())
        v = v_neg.clip(None, 0) + v_pos.clip(0, None)
        return v, v_neg_iters, v_pos_iters

    @torch.no_grad()
    def forward(self, batch):
        batch = _to_tensor(batch, self.device)
        u, *_ = self._solve_u(batch)
        if self.return_lazy:
            return u.cpu().numpy()
        else:
            return primal_solution(batch, u, self.v, self.epsilon).cpu().numpy()

    def training_step(self, batch, batch_idx):
        batch = _to_tensor(batch, self.device)

        u, u_neg_iters, u_pos_iters = self._solve_u(batch)
        self.log("u_neg_iters", float(u_neg_iters), prog_bar=True)
        self.log("u_pos_iters", float(u_pos_iters), prog_bar=True)

        v, v_neg_iters, v_pos_iters = self._solve_v(batch, u)
        self.log("v_neg_iters", float(v_neg_iters), prog_bar=True)
        self.log("v_pos_iters", float(v_pos_iters), prog_bar=True)

        loss = ((self.v - v)**2).mean() / 2
        self.log("train_loss", loss)
        return loss


def _to_tensor(batch, device=None):
    if hasattr(batch, "as_tensor"):
        return batch.as_tensor(device)
    elif isinstance(batch, list):
        return torch.vstack(batch).to(device)
    else:
        return torch.as_tensor(batch).to(device)
