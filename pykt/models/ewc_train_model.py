import os, sys
import torch
import torch.nn as nn
from torch.nn.functional import one_hot, binary_cross_entropy, cross_entropy
from torch.nn.utils.clip_grad import clip_grad_norm_
import numpy as np
from .evaluate_model import evaluate
from torch.autograd import Variable, grad
from ..utils.utils import debug_print
from pykt.config import que_type_models
import pickle
from torch.utils.data import DataLoader
import itertools
import time
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    BackwardPrefetch,
    ShardingStrategy,
    FullStateDictConfig,
    StateDictType,
)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

local_rank = 0
node_rank = 0


def rank0_print(*args):
    if local_rank == 0:
        print(*args)


def cal_loss(model, ys, r, rshft, sm, preloss=[]):
    model_name = model.module.model_name

    if model_name in ["gpt4kt","unikt"]:
        y = torch.masked_select(ys[0], sm)
        t = torch.masked_select(rshft, sm)
        # print(f"y: {y.shape}")
        # print(f"t: {t.shape}")
        loss1 = binary_cross_entropy(y.double(), t.double())

        if model.module.emb_type.find("predcurc") != -1:
            if model.module.emb_type.find("his") != -1:
                loss = (
                    model.module.l1 * loss1
                    + model.module.l2 * ys[1]
                    + model.module.l3 * ys[2]
                )
            else:
                loss = model.module.l1 * loss1 + model.module.l2 * ys[1]
        elif model.module.emb_type.find("predhis") != -1:
            loss = model.module.l1 * loss1 + model.module.l2 * ys[1]
        elif model.module.emb_type in ["qid_mt"]:
            loss = (1 - model.module.cf_weight) * loss1
            for cl_loss in preloss:
                # print(f"cl_loss:{cl_loss}")
                loss += cl_loss

        else:
            loss = loss1

    return loss


def model_forward(model, data, attn_grads=None):
    model_name = model.module.model_name
    # if model_name in ["dkt_forget", "lpkt"]:
    #     q, c, r, qshft, cshft, rshft, m, sm, d, dshft = data
    if (
        model_name in ["dkt_forget", "bakt_time"]
        or model.module.emb_type.find("time") != -1
    ):
        dcur, dgaps = data
    elif model_name in ["gpt4kt","unikt"] and model.module.emb_type.find("pt") != -1:
        dcur, dgaps = data
    else:
        dcur = data
    if model_name in ["dimkt"]:
        q, c, r, t, sd, qd = (
            dcur["qseqs"].to(device),
            dcur["cseqs"].to(device),
            dcur["rseqs"].to(device),
            dcur["tseqs"].to(device),
            dcur["sdseqs"].to(device),
            dcur["qdseqs"].to(device),
        )
        qshft, cshft, rshft, tshft, sdshft, qdshft = (
            dcur["shft_qseqs"].to(device),
            dcur["shft_cseqs"].to(device),
            dcur["shft_rseqs"].to(device),
            dcur["shft_tseqs"].to(device),
            dcur["shft_sdseqs"].to(device),
            dcur["shft_qdseqs"].to(device),
        )
    else:
        q, c, r, t = (
            dcur["qseqs"].to(device),
            dcur["cseqs"].to(device),
            dcur["rseqs"].to(device),
            dcur["tseqs"].to(device),
        )
        qshft, cshft, rshft, tshft = (
            dcur["shft_qseqs"].to(device),
            dcur["shft_cseqs"].to(device),
            dcur["shft_rseqs"].to(device),
            dcur["shft_tseqs"].to(device),
        )
    m, sm = dcur["masks"].to(device), dcur["smasks"].to(device)

    ys, preloss = [], []
    cq = torch.cat((q[:, 0:1], qshft), dim=1)
    cc = torch.cat((c[:, 0:1], cshft), dim=1)
    cr = torch.cat((r[:, 0:1], rshft), dim=1)
    if model_name in ["hawkes"]:
        ct = torch.cat((t[:, 0:1], tshft), dim=1)
    elif model_name in ["gpt4kt","unikt"]:
        if model.module.emb_type == "qid":
            y, y2, y3 = model(dcur, train=True)
        elif model.module.emb_type.find("pt") == -1:
            y, y2, y3, preloss = model(dcur, train=True)
        else:
            y, y2, y3, preloss = model(dcur, train=True, dgaps=dgaps)
        ys = [y[:, 1:], y2, y3]
        loss = cal_loss(model, ys, r, rshft, sm, preloss)

    elif model_name in que_type_models:
        y, loss = model.module.train_one_step(data)

    # if model_name in ["simplekt_sr"] and model.module.emb_type.find("mt") == -1:
    #     loss = cal_loss(model, ys, r, rshft, sm, preloss)
    if model_name not in [
        "atkt",
        "atktfix",
        "bakt_qikt",
    ] + que_type_models or model_name in ["gnn4kt"]:
        loss = cal_loss(model, ys, r, rshft, sm, preloss)
    return loss


def sample4cl(curtrain, batch_size, i, c0, max_epoch):
    # print(f"curtrain:{type(curtrain)}")
    rank0_print(f"curtrain:{len(curtrain)}")
    simple_size = min(1, i * (1 - c0) / max_epoch + c0)
    bn = simple_size // 64
    rank0_print(f"simple_size:{simple_size}")
    # print(f"simple_size:{int(simple_size*len(curtrain))}")
    # curtrain = curtrain[:2]
    # curtrain = dict(itertools.islice(curtrain.items(),int(simple_size*len(curtrain))))
    # curtrain = curtrain[1885]
    # curtrain = curtrain[:int(simple_size*len(curtrain))]
    # print(f"curtrain:{len(curtrain)}")
    # train_loader = DataLoader(curtrain, batch_size=batch_size)
    return simple_size, bn


def ewc_train_model(
    model,
    previous_loader,
    train_loader,
    valid_loader,
    num_epochs,
    opt,
    ckpt_path,
    lambda_ewc,
    test_loader=None,
    test_window_loader=None,
    save_model=False,
    dataset_name=None,
    fold=None,
    curtrain=None,
    batch_size=None,
    gradient_accumulation_steps=4.0,
    args=None,
    use_wandb=False,
):
    global local_rank, node_rank
    local_rank = args.local_rank
    node_rank = int(os.environ["RANK"])
    pretrain_epoch = args.pretrain_epoch
    max_auc, best_epoch = 0, -1
    train_step = 0
    best_model_path = None

    rel = None

    simple_size = 0
    cl_bn = 10000
    # 计算重要度举证
    params = {n: p for n, p in model.named_parameters() if p.requires_grad}
    _means = {} # 初始化要把参数限制在的参数域
    for n, p in params.items():
        _means[n] = p.data.clone().detach()
    precision_matrices = {} # 重要度
    for n, p in params.items():
        precision_matrices [n] = p.clone().detach().fill(0) # 取zeros_like
    model.eval()
    for j, data in enumerate(previous_loader):
        model.zero_grad()
        dcur, dgaps = data
        q, c, r, t = (
            dcur["qseqs"].to(device),
            dcur["cseqs"].to(device),
            dcur["rseqs"].to(device),
            dcur["tseqs"].to(device),
        )
        qshft, cshft, rshft, tshft = (
            dcur["shft_qseqs"].to(device),
            dcur["shft_cseqs"].to(device),
            dcur["shft_rseqs"].to(device),
            dcur["shft_tseqs"].to(device),
        )
    m, sm = dcur["masks"].to(device), dcur["smasks"].to(device)

    ys, preloss = [], []
    cq = torch.cat((q[:, 0:1], qshft), dim=1)
    cc = torch.cat((c[:, 0:1], cshft), dim=1)
    cr = torch.cat((r[:, 0:1], rshft), dim=1)
    if model.module.emb_type == "qid":
        y, y2, y3 = model(dcur, train=True)
    ys = [y[:, 1:], y2, y3]
    
    y = torch.masked_select(ys[0], sm)
    t = torch.masked_select(rshft, sm)
    loss1 = binary_cross_entropy(y.double(), t.double())
    loss1.backward()
    for n, p in model.named_parameters():
        precision_matrices[n].data += p.grad.data ** 2 / len(previous_loader)  # 计算重要度，以梯度的平方作为重要度
    
    model.train()
    for i in range(pretrain_epoch + 1, num_epochs + 1):
        start_time = time.time()
        loss_mean = []
        valid_loss_mean = []
        if model.module.emb_type.find("cl") != -1:
            # a = 1
            if simple_size != 1:
                simple_size, cl_bn = sample4cl(
                    curtrain, batch_size, i, model.module.c0, model.module.max_epoch
                )
        step = 0
        for j, data in enumerate(train_loader):
            step += 1
            # if j>=1: break
            if simple_size != 1 and j > cl_bn:
                continue
            if (
                model.module.model_name in que_type_models
                and model.module.model_name not in ["gnn4kt"]
            ):
                model.module.train()
            else:
                model.module.train()
            if model.module.model_name.find("bakt") != -1:
                if (
                    j == 0
                    or model.module.emb_type.find("grad") == -1
                    and model.module.emb_type != "qid"
                ):
                    attn_grads = None
                # if model.module.model_name.find("qikt") == -1:
                #     if j != 0:pre_attn_weights = model.module.attn_weights
                loss = model_forward(model, data, attn_grads)
            else:
                loss = model_forward(model, data, i)

            for n, p in model.named_parameters():
                _loss = precision_matrices[n] * (p - _means[n]) ** 2
                ewc_loss += _loss.sum()
            # ewc_loss + train_loss
            loss += lambda_ewc * ewc_loss
            loss = loss / gradient_accumulation_steps
            # print(f"loss:{loss}")
            loss.backward()  # compute gradients

            if (j + 1) % gradient_accumulation_steps == 0:
                opt.step()  # update model’s parameters
                opt.zero_grad()
            loss_mean.append(loss.detach().cpu().numpy())

        rank0_print(f"One epoch total step is {step}")
        loss_mean = np.mean(loss_mean)

        if model.module.model_name == "rkt":
            auc, acc = evaluate(model, valid_loader, model.module.model_name, rel)
        else:
            auc, acc, valid_loss = evaluate(model, valid_loader, model.module.model_name)

        validauc, validacc,valid_loss = auc, acc, valid_loss
        valid_loss = valid_loss / gradient_accumulation_steps
        valid_loss_mean.append(valid_loss.detach().cpu().numpy())
        valid_loss_mean = np.mean(valid_loss_mean)

        if save_model:
            # torch.save(
            #     model.module.state_dict(),
            #     os.path.join(
            #         ckpt_path, model.module.emb_type + "_model.module_{}.ckpt".format(i)
            #     )
            # )
            torch.distributed.barrier()

            save_policy = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)
            with FSDP.state_dict_type(
                        model, StateDictType.FULL_STATE_DICT, save_policy
                    ):
                cpu_state = model.state_dict()

            if local_rank == 0 and node_rank==0:
                save_name = os.path.join(ckpt_path, model.module.emb_type + "_model.module_{}.ckpt".format(i))
                torch.save(cpu_state, save_name)
        if auc > max_auc + 1e-3:
            max_auc = auc
            best_epoch = i
            if local_rank==0 and node_rank == 0:
                best_model_path = os.path.join(
                    ckpt_path, model.module.emb_type + "_model.module_{}.ckpt".format(i)
                    )

            testauc, testacc = -1, -1
            window_testauc, window_testacc = -1, -1
            # if not save_model:
            #     if test_loader != None:
            #         save_test_path = os.path.join(
            #             ckpt_path, model.module.emb_type + "_test_predictions.txt"
            #         )
            #         testauc, testacc = evaluate(
            #             model, test_loader, model.module.model_name, save_test_path
            #         )
            #     if test_window_loader != None:
            #         save_test_path = os.path.join(
            #             ckpt_path,
            #             model.module.emb_type + "_test_window_predictions.txt",
            #         )
            #         window_testauc, window_testacc = evaluate(
            #             model,
            #             test_window_loader,
            #             model.module.model_name,
            #             save_test_path,
            #         )
            # validauc, validacc = auc, acc
        end_time = time.time()
        rank0_print(
            f"Epoch: {i}, validauc: {validauc:.4}, validacc: {validacc:.4}, best epoch: {best_epoch}, best auc: {max_auc:.4}, train loss: {loss_mean}, emb_type: {model.module.emb_type}, model: {model.module.model_name}, save_dir: {ckpt_path}, duration: {end_time-start_time:.4}s"
        )
        rank0_print(
            f"            testauc: {round(testauc,4)}, testacc: {round(testacc,4)}, window_testauc: {round(window_testauc,4)}, window_testacc: {round(window_testacc,4)}"
        )

        if use_wandb:
            import wandb
            wandb.log( 
                    {"train loss": loss_mean, "validauc": validauc, "validacc": validacc}
                    )

        if i  > 50:
            break

    return (
        testauc,
        testacc,
        window_testauc,
        window_testacc,
        validauc,
        validacc,
        best_epoch,
    )
