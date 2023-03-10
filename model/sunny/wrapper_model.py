from typing import Any, Union

import torch
import torchmetrics
from torch import nn

import pytorch_lightning as pl
from torch.optim import Optimizer
# from torch.optim.lr_scheduler import _LRScheduler
from transformers import AutoModel, AutoConfig, get_cosine_schedule_with_warmup

from datasets.dataset import Feature
from model.sunny.aware_decoder import AwareDecoder


class WrapperModel(pl.LightningModule):
    def __init__(self,
                 bert_model: str = "roberta-base",
                 fine_tune: int = 0,
                 lr: float = 1e-5,
                 weight_decay: float = 0.0,
                 warmup_ratio: float = 0.1,
                 optimizer: str = "adamw",
                 constant_ids: list[torch.Tensor] = None,
                 operator_ids: list[torch.Tensor] = None,
                 num_training_steps: int = 150000,
                 label_pad_id: int = 1,
                 concat: bool = True,
                 dataset_config = None
                 ):
        super(WrapperModel, self).__init__()

        # equivalent automatic hyperparameter assignment
        # assign : self.hparams = {"bert_model": bert_model, "fine_tune": fine_tune ... } : dict[str, Any]
        self.save_hyperparameters()

        # set metric
        self.operator_accuracy = torchmetrics.Accuracy(task="multiclass",
                                                       num_classes=len(operator_ids))
        self.operand_accuracy = torchmetrics.Accuracy(task="multiclass",
                                                      num_classes=len(constant_ids)
                                                                  + self.hparams.datatset_config["max_numbers_size"]
                                                                  + self.hparams.datatset_config["max_operators_size"])
        self.loss = nn.CrossEntropyLoss()

        # set encoder
        self.encoder = AutoModel.from_pretrained(self.hparams["bert_model"])
        self.config = AutoConfig.from_pretrained(self.hparams["bert_model"])

        # pretrained language model은 fine-tuning하고 싶지 않을 때
        if not self.hparams["fine_tune"]:
            for param in self.encoder.parameters():
                param.requires_grad = False

        # set constant_list_embedding
        constant_vectors = self._get_vectors(constant_ids, concat=concat) # Tensor [N_C, H*2] or [N_C, H]
        # set operator_list_embedding
        operator_vectors = self._get_vectors(operator_ids, concat=concat) # Tensor [N_O, H*2] or [N_O, H]

        # set decoder
        self.decoder = AwareDecoder(input_hidden_dim=self.config.hidden_size,
                                    operator_vector=operator_vectors,
                                    const_vector=constant_vectors,
                                    operator_num=len(operator_ids),
                                    const_num=len(constant_ids),
                                    max_number_size=dataset_config["max_numbers_size"],
                                    max_equation=dataset_config["max_operators_size"],
                                    max_arity=max(map(max, dataset_config['operator_dict'].values())),
                                    label_pad_id=label_pad_id,
                                    concat=concat)

    def _get_vectors(self, ids_list: list[torch.Tensor], concat: bool) -> torch.Tensor:
        # return the sum or concatenation of first and last hidden_state of constant_ids
        # ids_list can be constant_ids or operator_ids
        vectors = [] # list(torch.Tensor[H]) or list(torch.Tensor[H*2]) according to concat
        for ids in ids_list:
            if concat:
                # 만약 첫번째 id와 마지막 id가 같은 const_XXX의 경우에는 구분할 수 없다는 문제가 존재
                vectors.append(torch.cat((self.encoder(ids.unsqueeze(0)).last_hidden_state[0, 0, :],
                               self.encoder(ids.unsqueeze(0)).last_hidden_state[0, -1, :])))
            else:
                vectors.append(self.encoder(ids.unsqueeze(0)).last_hidden_state[0, 0, :] +
                               self.encoder(ids.unsqueeze(0)).last_hidden_state[0, -1, :])

        return torch.stack(vectors) # [N_C, H*2] or [N_C, H] according to concat


    def forward(self, x: Feature):
        encoder_output = self.encoder(x.input_ids).last_hidden_state
        operator_logit, operand_logit = self.decoder(encoder_output, x.attention_mask, x.question_mask, x.number_mask)

        return operator_logit, operand_logit  # [[B, T, N_O], [B, T, A, N_D]] : Operator, Operand prediction

    def _calculate_operator_loss(self, logits: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
        bsz, max_operator_len, _ = logits.shape #[B, T, N_O]

        operator_logit_flatten = torch.reshape(logits, (bsz * max_operator_len, -1))  # [B*T, N_O]
        gold_operator_label_flatten = torch.reshape(labels, (-1,))  # [B*T]

        assert operator_logit_flatten.shape[0] == gold_operator_label_flatten.shape[0]
        assert len(operator_logit_flatten.shape) == 2 and len(gold_operator_label_flatten.shape) == 1

        return self.loss(operator_logit_flatten, gold_operator_label_flatten)


    def _calculate_operand_loss(self, logits: torch.Tensor, labels: torch.Tensor) -> torch.Tensor:
        bsz, max_operator_len, max_arity, _ = logits.shape  # [B, T, A, N_D]

        operand_logit_flatten = torch.reshape(torch.reshape(logits, (bsz, max_operator_len * max_arity, -1)),
                                              (bsz * max_operator_len * max_arity, -1))  # [B*T*A, N_D]
        gold_operand_label_flatten = torch.reshape(torch.reshape(labels, (bsz, -1)), (-1,))  # [B*T*A]

        assert operand_logit_flatten.shape[0] == gold_operand_label_flatten.shape[0]
        assert len(operand_logit_flatten.shape) == 2 and len(gold_operand_label_flatten.shape) == 1

        return self.loss(operand_logit_flatten, gold_operand_label_flatten)

    def training_step(self, batch: Feature, batch_idx: int) -> torch.Tensor:
        gold_operator_label = batch.operator_label - 1 # 0 is reserved for unknown, 1 is padding included in loss
        gold_operand_label = batch.operand_label - 1 # 0 is reserved for unknown, 1 is padding included in loss

        operator_logit, operand_logit = self(batch)    #[B, T, N_O + 1], [B, T, A, N_D + 1]

        operator_loss = self._calculate_operator_loss(operator_logit, gold_operator_label)
        operand_loss = self._calculate_operand_loss(operand_logit, gold_operand_label)

        self.log("operator_accuracy", self.operator_accuracy(operator_logit, gold_operator_label), on_step=True)
        self.log("operand_accuracy", self.operand_accuracy(operand_logit, gold_operand_label), on_step=True)
        self.log("operator_loss", operator_loss, on_step=True)
        self.log("operand_loss", operand_loss, on_step=True)

        loss = operator_loss + operand_loss

        return loss

    def configure_optimizers(self) -> tuple[list[Optimizer], list["_LRScheduler"]]:
        optims = []
        schedulers = []
        if self.hparams.optimizer == "adam":
            optim = torch.optim.Adam(self.parameters(), lr=self.hparams.lr)
        elif self.hparams.optimizer == "adamw":
            optim = torch.optim.AdamW(self.parameters(), lr=self.hparams.lr)
        elif self.hparams.optimizer == "sgd":
            optim = torch.optim.SGD(self.parameters(), lr=self.hparams.lr)
        else:
            raise NotImplementedError

        optims.append(optim)
        schedulers.append(get_cosine_schedule_with_warmup(optim,
                                        num_warmup_steps=self.hparams.warmup_ratio * self.hparams.num_training_steps,
                                        num_training_steps=self.hparams.num_training_steps))

        return optims, schedulers
