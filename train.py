import argparse
import math
import os
import random
import shutil
import time
from copy import deepcopy
from collections import OrderedDict

import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data as data
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import dataset.cifar as dataset
from utils import Logger, AverageMeter, accuracy, mkdir_p

parser = argparse.ArgumentParser(description='PyTorch FixMatch Training')
parser.add_argument('--gpu-id', default='0', type=int,
                    help='id(s) for CUDA_VISIBLE_DEVICES')
parser.add_argument('--dataset', default='cifar10', type=str,
                    choices=['cifar10', 'cifar100'],
                    help='dataset name')
parser.add_argument('--num-labeled', type=int, default=4000,
                    help='Number of labeled data')
parser.add_argument('--arch', default='widereset', type=str,
                    choices=['wideresnet', 'resnext'],
                    help='dataset name')
parser.add_argument('--num-workers', type=int, default=4,
                    help='Number of workers')
parser.add_argument('--epochs', default=1024, type=int,
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int,
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--batch-size', default=64, type=int,
                    help='train batchsize')
parser.add_argument('--lr', '--learning-rate', default=0.03, type=float,
                    help='initial learning rate')
parser.add_argument('--warmup', default=5, type=float,
                    help='warmup epochs (unlabeled data based)')
parser.add_argument('--wdecay', default=5e-4, type=float,
                    help='weight decay')
parser.add_argument('--nesterov', action='store_true', default=True,
                    help='use nesterov momentum')
parser.add_argument('--resume', default='', type=str,
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--seed', type=int, default=-1,
                    help="random seed (-1: don't use random seed)")
parser.add_argument('--iteration', type=int, default=-1,
                    help='Number of iterations '
                    '(-1: automatic calculation to learn 65536 examples.)')
parser.add_argument('--out', default='result',
                    help='Directory to output the result')
parser.add_argument('--threshold', default=0.95, type=float,
                    help='pseudo label threshold')
parser.add_argument('--lambda-u', default=1, type=float,
                    help='unlabeled loss weight')
parser.add_argument('--mu', default=7, type=float,
                    help='coefficient of unlabeled batch size')
parser.add_argument('--use-ema', action='store_true', default=True,
                    help='use EMA model')
parser.add_argument('--no-progress', action='store_true',
                    help="don't use prgress bar")
parser.add_argument('--ema-decay', default=0.999, type=float,
                    help='EMA decay rate')
parser.add_argument("--local_rank", type=int, default=-1,
                    help="For distributed training: local_rank")

args = parser.parse_args()
state = {k: v for k, v in args._get_kwargs()}
device = torch.device("cuda", args.gpu_id)

if args.dataset == 'cifar10':
    num_classes = 10
    get_dataset = dataset.get_cifar10
    if args.arch == 'wideresnet':
        depth = 28
        widen_factor = 2
    if args.arch == 'resnext':
        cardinality = 4
        depth = 28
        width = 4

elif args.dataset == 'cifar100':
    num_classes = 100
    get_dataset = dataset.get_cifar100
    if args.arch == 'wideresnet':
        depth = 28
        widen_factor = 10
    if args.arch == 'resnext':
        cardinality = 8
        depth = 29
        width = 64

if args.seed != -1:
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

best_acc = 0


def create_model(arch):
    if arch == 'wideresnet':
        import models.wideresnet as models
        model = models.build_wideresnet(depth=depth,
                                        widen_factor=widen_factor,
                                        dropout=0,
                                        num_classes=num_classes).to(device)
    elif arch == 'resnext':
        import models.resnext as models
        model = models.build_resnext(cardinality=cardinality,
                                     depth=depth,
                                     width=width,
                                     num_classes=num_classes).to(device)
    print('Total params: {:.2f}M'.format(
        sum(p.numel() for p in model.parameters())/1e6))
    return model


def get_cosine_schedule_with_warmup(optimizer,
                                    num_warmup_steps,
                                    num_training_steps,
                                    num_cycles=7./16.,
                                    last_epoch=-1):
    def _lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        no_progress = float(current_step - num_warmup_steps) / \
            float(max(1, num_training_steps - num_warmup_steps))
        return max(0., math.cos(math.pi * num_cycles * no_progress))

    return LambdaLR(optimizer, _lr_lambda, last_epoch)


def main():
    global best_acc

    if not os.path.isdir(args.out):
        mkdir_p(args.out)

    train_labeled_set, train_unlabeled_set, test_set = get_dataset(
        './data', args.num_labeled, num_classes=num_classes)

    labeled_trainloader = data.DataLoader(
        train_labeled_set, batch_size=args.batch_size, shuffle=True,
        num_workers=args.num_workers, drop_last=True, pin_memory=False)

    unlabeled_trainloader = data.DataLoader(
        train_unlabeled_set, batch_size=args.batch_size*args.mu, shuffle=True,
        num_workers=args.num_workers, drop_last=True, pin_memory=False)

    test_loader = data.DataLoader(
        test_set, batch_size=args.batch_size, shuffle=False,
        num_workers=args.num_workers, pin_memory=False)

    model = create_model(args.arch)

    no_decay = ["bias", "bn"]
    grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.wdecay * 2,  # L2 regulization
        },
        {"params": [p for n, p in model.named_parameters() if any(
            nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]

    optimizer = optim.SGD(grouped_parameters, lr=args.lr,
                          momentum=0.9,
                          nesterov=args.nesterov)

    ema_model = None
    if args.use_ema:
        ema_model = ModelEMA(model, args.ema_decay, device)

    if args.iteration == -1:
        args.iteration = int(65536//args.batch_size)

    total_step = args.epochs * args.iteration
    scheduler = get_cosine_schedule_with_warmup(
        optimizer, args.warmup * len(unlabeled_trainloader), total_step)

    start_epoch = 0

    if args.resume:
        print('==> Resuming from checkpoint..')
        assert os.path.isfile(
            args.resume), 'Error: no checkpoint directory found!'
        args.out = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        best_acc = checkpoint['best_acc']
        start_epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['state_dict'])
        ema_model.ema.load_state_dict(checkpoint['ema_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        logger = Logger(os.path.join(args.out, 'log.txt'),
                        title=args.dataset, resume=True)
    else:
        logger = Logger(os.path.join(args.out, 'log.txt'), title=args.dataset)
        logger.set_names(['Train Loss', 'Train Loss X', 'Train Loss U',
                          'Test Loss', 'Test Acc.'])

    writer = SummaryWriter(args.out)
    step = 0
    test_accs = []
    model.zero_grad()

    if args.no_progress:
        print('==> Training...')

    for epoch in range(start_epoch, args.epochs):

        train_loss, train_loss_x, train_loss_u, mask_prob = train(
            labeled_trainloader, unlabeled_trainloader, model,
            optimizer, ema_model, scheduler, epoch)

        if args.no_progress:
            print('Epoch {}. train_loss: {:.4f}. train_loss_x: {:.4f}. train_loss_u: {:.4f}.'
                  .format(epoch+1, train_loss, train_loss_x, train_loss_u))

        if args.use_ema:
            test_model = ema_model.ema
        else:
            test_model = model

        test_loss, test_acc = test(test_loader, test_model, epoch)

        step = args.iteration * epoch
        writer.add_scalar('losses/1.train_loss', train_loss, step)
        writer.add_scalar('losses/2.train_loss_x', train_loss_x, step)
        writer.add_scalar('losses/3.train_loss_u', train_loss_u, step)
        writer.add_scalar('losses/4.mask', mask_prob, step)
        writer.add_scalar('losses/5.test_loss', test_loss, step)
        writer.add_scalar('accuracy/test_acc', test_acc, step)

        logger.append([train_loss, train_loss_x, train_loss_u,
                       test_loss, test_acc])

        is_best = test_acc > best_acc
        best_acc = max(test_acc, best_acc)
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'ema_state_dict': ema_model.ema.state_dict() if args.use_ema else None,
            'acc': test_acc,
            'best_acc': best_acc,
            'optimizer': optimizer.state_dict(),
        }, is_best)
        test_accs.append(test_acc)
        print(f'Best top-1 acc: {best_acc}')
        print('Median top-1 acc: {:.2f}\n'.format(np.median(test_accs[-20:])))

    logger.close()
    writer.close()


def train(labeled_trainloader, unlabeled_trainloader, model,
          optimizer, ema_model, scheduler, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_u = AverageMeter()
    end = time.time()

    labeled_train_iter = iter(labeled_trainloader)
    unlabeled_train_iter = iter(unlabeled_trainloader)
    model.train()

    p_bar = range(args.iteration)
    if not args.no_progress:
        p_bar = tqdm(p_bar,
                     disable=args.local_rank not in [-1, 0])

    for batch_idx in p_bar:
        try:
            inputs_x, targets_x = labeled_train_iter.next()
        except:
            labeled_train_iter = iter(labeled_trainloader)
            inputs_x, targets_x = labeled_train_iter.next()

        try:
            (inputs_u_w, inputs_u_s), _ = unlabeled_train_iter.next()
        except:
            unlabeled_train_iter = iter(unlabeled_trainloader)
            (inputs_u_w, inputs_u_s), _ = unlabeled_train_iter.next()

        data_time.update(time.time() - end)
        batch_size = inputs_x.shape[0]
        inputs = torch.cat((inputs_x, inputs_u_w, inputs_u_s)).to(device)
        logits = model(inputs)
        logits_x = logits[:batch_size]
        logits_u_w, logits_u_s = logits[batch_size:].chunk(2)
        targets_x = targets_x.to(device, non_blocking=True)
        del logits

        Lx = F.cross_entropy(logits_x, targets_x, reduction='mean')

        pseudo_label = torch.softmax(logits_u_w, dim=-1).detach()
        max_probs, targets_u = torch.max(pseudo_label, dim=-1)
        mask = max_probs.ge(args.threshold).float()

        Lu = (F.cross_entropy(logits_u_s, targets_u,
                              reduction='none') * mask).mean()

        loss = Lx + args.lambda_u * Lu

        losses.update(loss.item(), batch_size * (1+args.mu*2))
        losses_x.update(Lx.item(), batch_size * (1+args.mu*2))
        losses_u.update(Lu.item(), batch_size * (1+args.mu*2))

        loss.backward()
        optimizer.step()
        scheduler.step()
        if ema_model is not None:
            ema_model.update(model)
        model.zero_grad()

        batch_time.update(time.time() - end)
        end = time.time()
        mask_prob = mask.mean().item()
        if not args.no_progress:
            p_bar.set_description('Train Epoch: {epoch}/{epochs:4}. Iter: {batch:4}/{iter:4}. LR: {lr:.6f}. Data: {data:.3f}s. Batch: {bt:.3f}s. Loss: {loss:.4f}. Loss_x: {loss_x:.4f}. Loss_u: {loss_u:.4f}. Mask: {mask:.4f}. '.format(
                epoch=epoch + 1,
                epochs=args.epochs,
                batch=batch_idx + 1,
                iter=args.iteration,
                lr=scheduler.get_last_lr()[0],
                data=data_time.avg,
                bt=batch_time.avg,
                loss=losses.avg,
                loss_x=losses_x.avg,
                loss_u=losses_u.avg,
                mask=mask_prob,
            ))
    if not args.no_progress:
        p_bar.close()
    return losses.avg, losses_x.avg, losses_u.avg, mask_prob


def test(test_loader, model, epoch):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    p_bar = test_loader
    if not args.no_progress:
        p_bar = tqdm(test_loader,
                     disable=args.local_rank not in [-1, 0])

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(p_bar):
            data_time.update(time.time() - end)
            model.eval()

            inputs = inputs.to(device)
            targets = targets.to(device, non_blocking=True)
            outputs = model(inputs)
            loss = F.cross_entropy(outputs, targets)

            prec1, prec5 = accuracy(outputs, targets, topk=(1, 5))
            losses.update(loss.item(), inputs.shape[0])
            top1.update(prec1.item(), inputs.shape[0])
            top5.update(prec5.item(), inputs.shape[0])

            batch_time.update(time.time() - end)
            end = time.time()
            if not args.no_progress:
                p_bar.set_description('Test Iter: {batch:4}/{iter:4}. Data: {data:.3f}s. Batch: {bt:.3f}s. Loss: {loss:.4f}. top1: {top1:.2f}. top5: {top5:.2f}. '.format(
                    batch=batch_idx + 1,
                    iter=len(test_loader),
                    data=data_time.avg,
                    bt=batch_time.avg,
                    loss=losses.avg,
                    top1=top1.avg,
                    top5=top5.avg,
                ))
        if not args.no_progress:
            p_bar.close()
    print("top-1 acc: {:.2f}. top-5 acc: {:.2f}.".format(top1.avg, top5.avg))
    return (losses.avg, top1.avg)


def save_checkpoint(state, is_best, checkpoint=args.out, filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(
            checkpoint, 'model_best.pth.tar'))


class ModelEMA(object):
    def __init__(self, model, decay, device='', resume=''):
        self.ema = deepcopy(model)
        self.ema.eval()
        self.decay = decay
        self.device = device
        if device:
            self.ema.to(device=device)
        self.ema_has_module = hasattr(self.ema, 'module')
        if resume:
            self._load_checkpoint(resume)
        for p in self.ema.parameters():
            p.requires_grad_(False)

    def _load_checkpoint(self, checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        assert isinstance(checkpoint, dict)
        if 'ema_state_dict' in checkpoint:
            new_state_dict = OrderedDict()
            for k, v in checkpoint['ema_state_dict'].items():
                if self.ema_has_module:
                    name = 'module.' + k if not k.startswith('module') else k
                else:
                    name = k
                new_state_dict[name] = v
            self.ema.load_state_dict(new_state_dict)

    def update(self, model):
        needs_module = hasattr(model, 'module') and not self.ema_has_module
        with torch.no_grad():
            msd = model.state_dict()
            for k, ema_v in self.ema.state_dict().items():
                if needs_module:
                    k = 'module.' + k
                model_v = msd[k].detach()
                if self.device:
                    model_v = model_v.to(device=self.device)
                ema_v.copy_(ema_v * self.decay + (1. - self.decay) * model_v)


if __name__ == '__main__':
    cudnn.benchmark = True
    main()
