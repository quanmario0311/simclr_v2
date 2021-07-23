import math
from argparse import ArgumentParser

import torch
from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint
from torch import nn, Tensor
from torch.nn import functional as F
import module.resnet as resnet
from pl_bolts.models.self_supervised.resnets import resnet18, resnet50
from pl_bolts.optimizers.lars import LARS
from pl_bolts.optimizers.lr_scheduler import linear_warmup_decay
from pl_bolts.transforms.dataset_normalizations import (
    cifar10_normalization,
    imagenet_normalization,
    stl10_normalization,
)
from ContrastiveLoss import ContrastiveLoss
import numpy as np
class SyncFunction(torch.autograd.Function):

    @staticmethod
    def forward(ctx, tensor):
        ctx.batch_size = tensor.shape[0]

        gathered_tensor = [torch.zeros_like(tensor) for _ in range(torch.distributed.get_world_size())]

        torch.distributed.all_gather(gathered_tensor, tensor)
        gathered_tensor = torch.cat(gathered_tensor, 0)

        return gathered_tensor

    @staticmethod
    def backward(ctx, grad_output):
        grad_input = grad_output.clone()
        torch.distributed.all_reduce(grad_input, op=torch.distributed.ReduceOp.SUM, async_op=False)

        idx_from = torch.distributed.get_rank() * ctx.batch_size
        idx_to = (torch.distributed.get_rank() + 1) * ctx.batch_size
        return grad_input[idx_from:idx_to]


class Projection(nn.Module):

    def __init__(self, input_dim=2048, hidden_dim=2048, output_dim=128):
        super().__init__()
        self.output_dim = output_dim
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        self.model = nn.Sequential(
            nn.Linear(self.input_dim, self.hidden_dim), nn.BatchNorm1d(self.hidden_dim), nn.ReLU(),
            nn.Linear(self.hidden_dim, self.output_dim, bias=False)
        )

    def forward(self, x):
        x = self.model(x)
        return F.normalize(x, dim=1)


class SimCLR(LightningModule):

    def __init__(
        self,
        num_samples: int = 16,
        batch_size: int = 32,
        gpus: int = 1,
        num_nodes: int = 1,
        arch: str = 'resnet18', #
        hidden_mlp: int = 2048, #
        feat_dim: int = 128,
        warmup_epochs: int = 10,
        max_epochs: int = 100,
        temperature: float = 0.1,
        mode: str = None,
        maxpool1: bool = True,
        optimizer: str = 'adam',
        exclude_bn_bias: bool = False,
        start_lr: float = 0.,
        learning_rate: float = 3e-4,
        final_lr: float = 0.,
        weight_decay: float = 1e-6,
        **kwargs
    ):
        """
        Args:
            batch_size: the batch size
            num_samples: num samples in the dataset
            warmup_epochs: epochs to warmup the lr for
            lr: the optimizer learning rate
            opt_weight_decay: the optimizer weight decay
            loss_temperature: the loss temperature
        """
        super().__init__()

        self.gpus = gpus
        self.num_nodes = num_nodes
        self.arch = arch
        self.num_samples = num_samples
        self.batch_size = batch_size

        self.hidden_mlp = hidden_mlp
        self.feat_dim = feat_dim
        self.mode = mode
        self.maxpool1 = maxpool1

        self.optim = optimizer
        self.exclude_bn_bias = exclude_bn_bias
        self.weight_decay = weight_decay
        self.temperature = temperature

        self.start_lr = start_lr
        self.final_lr = final_lr
        self.learning_rate = learning_rate
        self.warmup_epochs = warmup_epochs
        self.max_epochs = max_epochs

        self.train_loss = []

        self.encoder = self.init_model()
        if arch == 'resnet50':
            self.features = nn.Linear(self.hidden_mlp, self.hidden_mlp) #First Projection Head
            self.batch_norm1d = nn.BatchNorm1d(self.hidden_mlp)
            self.projection = Projection(input_dim=self.hidden_mlp, hidden_dim=self.hidden_mlp, output_dim=self.feat_dim)
        elif arch == 'resnet18':
            self.features = nn.Linear(512, 512) #First Projection Head
            self.batch_norm1d = nn.BatchNorm1d(512)
            self.projection = Projection(input_dim=512, hidden_dim=512, output_dim=self.feat_dim)

        global_batch_size = self.num_nodes * self.gpus * self.batch_size if self.gpus > 0 else self.batch_size
        self.train_iters_per_epoch = self.num_samples // global_batch_size

    def init_model(self):
        if self.arch == 'resnet18':
            # backbone = resnet.resnet18(mode=self.mode)
            backbone = resnet.resnet18(mode=self.mode)

        elif self.arch == 'resnet50':
            backbone = resnet.resnet50(mode=self.mode)

        return backbone
    
    def forward(self, x):
        x = self.encoder(x)
        flatten = torch.nn.Flatten()
        x = flatten(x)
        return self.features(x)
    
    def nt_xent_loss(self, out_1, out_2, temperature, eps=1e-6):
        """
            assume out_1 and out_2 are normalized
            out_1: [batch_size, dim]
            out_2: [batch_size, dim]
        """
        # gather representations in case of distributed training
        # out_1_dist: [batch_size * world_size, dim]
        # out_2_dist: [batch_size * world_size, dim]
        if torch.distributed.is_available() and torch.distributed.is_initialized():
            out_1_dist = SyncFunction.apply(out_1)
            out_2_dist = SyncFunction.apply(out_2)
        else:
            out_1_dist = out_1
            out_2_dist = out_2

        # out: [2 * batch_size, dim]
        # out_dist: [2 * batch_size * world_size, dim]
        out = torch.cat([out_1, out_2], dim=0)
        out_dist = torch.cat([out_1_dist, out_2_dist], dim=0)

        # cov and sim: [2 * batch_size, 2 * batch_size * world_size]
        # neg: [2 * batch_size]
        cov = torch.mm(out, out_dist.t().contiguous())
        sim = torch.exp(cov / temperature)
        neg = sim.sum(dim=-1)

        # from each row, subtract e^(1/temp) to remove similarity measure for x1.x1
        row_sub = Tensor(neg.shape).fill_(math.e**(1 / temperature)).to(neg.device)
        neg = torch.clamp(neg - row_sub, min=eps)  # clamp for numerical stability

        # Positive similarity, pos becomes [2 * batch_size]
        pos = torch.exp(torch.sum(out_1 * out_2, dim=-1) / temperature)
        pos = torch.cat([pos, pos], dim=0)

        loss = -torch.log(pos / (neg + eps)).mean()

        return loss

    def shared_step(self, batch):
        (img1, img2, _), y = batch

        features_1 = self(img1)
        features_2 = self(img2)

        features_1 = F.relu(self.batch_norm1d(features_1))
        features_2 = F.relu(self.batch_norm1d(features_2))

        features_1 = self.projection(features_1)
        features_2 = self.projection(features_2)
        # batch_size = y.shape[0]//2
        # features = self.projection(features)
        # f1, f2 = torch.split(features, (batch_size,batch_size), dim=0)
        # features = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1)
        
        loss = self.nt_xent_loss(features_1,features_2,0.5)

        return loss

    def training_step(self, batch, batch_idx):
        loss = self.shared_step(batch)
        self.train_loss.append(loss)

        self.log('train_loss', loss, on_step=True, prog_bar=True, on_epoch=False)


        return {"loss": loss, 'log': {'Loss/train': loss}} 

    def validation_step(self, batch, batch_idx):
        loss = self.shared_step(batch)

        self.log('val_loss', loss, on_step=False, prog_bar=True, on_epoch=True, sync_dist=True)
        return {"loss": loss}
    
    def on_train_epoch_end(self):
        print('training end: \n')
        avg_train_loss = sum(self.train_loss) / len(self.train_loss)
        print("average train loss",avg_train_loss)
        # avg_loss = torch.stack([x['train_loss'] for x in outputs]).mean()
        # self.log('avg_train_loss', avg_loss, on_step=False, sync_dist=True)
        # return {'avg_train_loss': avg_loss, 'log': {'Loss/avg_train_loss': avg_loss}}
        self.log('avg_train_loss', avg_train_loss)
        self.train_loss = []

    def configure_optimizers(self):
        if self.exclude_bn_bias:
            params = self.exclude_from_wt_decay(self.named_parameters(), weight_decay=self.weight_decay)
        else:
            params = self.parameters()

        if self.optim == 'lars':
            optimizer = LARS(
                params,
                momentum=0.9,
                weight_decay=self.weight_decay,
                trust_coefficient=0.001,
            )
        elif self.optim == 'adam':
            optimizer = torch.optim.Adam(params, lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.optim == 'adamw':
            optimizer = torch.optim.AdamW(params, weight_decay=self.weight_decay)

        warmup_steps = self.train_iters_per_epoch * self.warmup_epochs
        total_steps = self.train_iters_per_epoch * self.max_epochs

        scheduler = {
            "scheduler": torch.optim.lr_scheduler.LambdaLR(
                optimizer,
                linear_warmup_decay(warmup_steps, total_steps, cosine=True),
            ),
            "interval": "step",
            "frequency": 1,
        }

        return {"optimizer": optimizer, "lr_scheduler": scheduler}


    

    
    
