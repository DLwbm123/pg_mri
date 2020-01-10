import logging
import time
import datetime
import random
import argparse
import pathlib
from collections import defaultdict

import numpy as np
import torch
import torch.nn.functional as F
from torch.autograd import Variable
from tensorboardX import SummaryWriter

# Importing Arguments is required for loading of Gauss reconstruction model
from src.recon_models.unet_dist_model import Arguments

from src.helpers.torch_metrics import ssim
from src.helpers.metrics import Metrics, METRIC_FUNCS
from src.helpers.utils import (add_mask_params, save_json, check_args_consistency, count_parameters,
                               count_trainable_parameters, count_untrainable_parameters, plot_grad_flow)
from src.helpers.data_loading import create_data_loaders
from src.recon_models.recon_model_utils import (acquire_new_zf_exp_batch, acquire_new_zf_batch,
                                                recon_model_forward_pass, load_recon_model)
from src.impro_models.impro_model_utils import (load_impro_model, build_impro_model, build_optim, save_model,
                                                impro_model_forward_pass)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

torch.autograd.set_detect_anomaly(True)


targets = defaultdict(lambda: defaultdict(lambda: 0))
target_counts = defaultdict(lambda: defaultdict(lambda: 0))
outputs = defaultdict(lambda: defaultdict(list))


def get_pred_and_target(args, kspace, masked_kspace, mask, gt, mean, std, model, recon_model, recon_output, eps, k):
    recon = recon_output[:, 0:1, ...]  # Other channels are uncertainty maps + other input to the impro model
    norm_recon = recon * std + mean  # Back to original scale for metric  # TODO: is this unnormalisation necessary?

    # shape = batch
    base_score = ssim(norm_recon, gt, size_average=False, data_range=1e-4).mean(-1).mean(-1)  # keep channel dim = 1

    # Create improvement targets for this batch
    # TODO: Currently we take the target to be equal to the output, except for a few that we actually compute. This
    #  results in zero gradient for all non-computed targets.
    #  Previously we wanted to not even have to compute these 0 gradients (maybe more efficient?), and so we
    #  started with a zero matrix of size batch x resolution for both output_train and target, and merely input
    #  the values we wanted to train (so the other entries would be zero (not important), and have no gradient).
    #  Unfortunately, PyTorch doesn't like it when we do in place operations to our output_train, because somehow
    #  the gradients get lost that way...
    # Impro model output (Q(a|s) is used to select which targets to actually compute for fine-tuning Q(a|s)
    # squeeze mask such that batch dimension doesn't vanish if it's 1.
    output = impro_model_forward_pass(args, model, recon_output, mask.squeeze(1).squeeze(1).squeeze(-1))

    acquired = (mask.squeeze(1).squeeze(1).squeeze(-1) == 1)
    topk_rows = torch.zeros((mask.size(0), k)).long().to(args.device)
    # Now set random rows to overwrite topk values and inds (epsilon greedily)
    for i in range(mask.size(0)):  # Loop over slices in batch
        # Get k top indices that are not acquired rows, because we can set targets for acquired rows to 0
        # TODO: this may bias the model, because it learns to predict those 0s much more often than other values
        sl_remain_out = output[i, :].clone().detach()

        # TODO: Set impros for all acquired rows to zero for training? If so, uncomment this.
        # sl_acq = acquired[i, :].nonzero().flatten()
        # sl_remain_out[sl_acq] = -1e3  # so it doesn't get chosen, but indices stay correct

        _, sl_topk_rows = torch.topk(sl_remain_out, k)
        topk_rows[i, :] = sl_topk_rows

        # Some of these will be replaced by random rows: select which ones to replace
        topk_replace = (eps > torch.rand(k))  # which of the top k rows to replace with random rows (binary mask)
        topk_replace_inds = topk_replace.nonzero().flatten()  # which of the top k rows to replace with random rows

        # Which random rows to pick to replace the some of the topk rows (sampled without replacement here)
        # TODO: moving to device should happen only once before loop. Should just mutate object after?
        # TODO: Set impros for all acquired rows to zero for training? If so, uncomment this, comment other.
        # row_options = torch.tensor([idx for idx in range(mask.size(-2)) if
        #                             (idx not in sl_topk_rows and idx not in sl_acq)]).to(args.device)
        row_options = torch.tensor([idx for idx in range(mask.size(-2)) if
                                    idx not in sl_topk_rows]).to(args.device)
        # Shuffle potential rows to acquire random rows (equal to the number of 1s in topk_replace)
        randrows = row_options[torch.randperm(len(row_options))][:topk_replace.sum()].long()
        # If there are not topk_replace.sum() rows left as options (for instance because we set k == resolution: this
        # would lead to 0 options left) then row_options has length less than topk_replace_inds. This means the below
        # assignment will fail. Hence we clip the length of the latter. We shuffle the topk_replace_inds, so that we
        # don't replace rows the model likes more often than those the model doesn't like (topk_replace_inds is ordered
        # from highest scoring to lowest scoring.
        topk_replace_inds = topk_replace_inds[torch.randperm(len(topk_replace_inds))][:len(randrows)]

        # TODO: Currently the topk_rows are determined, and then a number of them are overwritten by random rows NOT
        #  IN THE TOPK. This should be fixed so that topk rows can also be randomly selected (otherwise bias).
        # TODO: Not setting targets to 0 for already acquired rows seems to work better: these rows are otherwise
        #  overrepresented, heavily biasing the learning towards trying to predict those at the expense of other rows.
        #  The result is outshoots for some rows (this effect is heavily mitigated by sampling more rows at a time,
        #  as the bias is much less powerful when other rows also get sampled about half the time. Sampling more is
        #  however exactly what we are trying to avoid).
        topk_rows[i, topk_replace_inds] = randrows

    # Acquire chosen rows, and compute the improvement target for each (batched)
    # shape = batch x rows = k x res x res
    zf_exp, mean_exp, std_exp = acquire_new_zf_exp_batch(kspace, masked_kspace, topk_rows)
    # shape = batch . rows x 1 x res x res, so that we can run the forward model for all rows in the batch
    zf_input = zf_exp.view(mask.size(0) * k, 1, mask.size(-2), mask.size(-2))
    # shape = batch . rows x 2 x res x res
    recons_output = recon_model_forward_pass(args, recon_model, zf_input)
    # shape = batch . rows x 1 x res x res, extract reconstruction to compute target
    recons = recons_output[:, 0:1, ...]
    # shape = batch x rows x res x res
    recons = recons.view(mask.size(0), k, mask.size(-2), mask.size(-2))
    norm_recons = recons * std_exp + mean_exp  # TODO: Normalisation necessary?
    gt = gt.expand(-1, k, -1, -1)
    # scores = batch x rows (channels), base_score = batch x 1
    scores = ssim(norm_recons, gt, size_average=False, data_range=1e-4).mean(-1).mean(-1)
    impros = (scores - base_score).detach()  # .detach() only necessary when recon_output.requires_grad == True
    # target = batch x rows, topk_rows and impros = batch x k
    target = output.clone().detach()
    loss_mask = torch.zeros_like(target).to(args.device)
    for j, sl_topk_rows in enumerate(topk_rows):
        # impros[j, 0] (slice j, row 0 in sl_topk_rows[j]) corresponds to the row sl_topk_rows[j, 0] = 9 (for instance)
        # This means the improvement 9th row in the kspace ordering is element 0 in impros.
        kspace_row_inds, permuted_inds = sl_topk_rows.sort()
        target[j, kspace_row_inds] = impros[j, permuted_inds]
        # TODO: Set impros for all acquired rows to 0 during training? If so, uncomment this.
        # zero_inds = (mask[j, 0, 0, :, 0] == 1).nonzero().flatten()
        # target[j, zero_inds] = 0.
        # loss_mask[j, zero_inds] = 1.
        loss_mask[j, kspace_row_inds] = 1.
    return target, output, loss_mask


def train_epoch(args, epoch, recon_model, model, train_loader, optimiser, writer, eps, k):
    # TODO: try batchnorm in FC layers
    model.train()
    epoch_loss = [0. for _ in range(args.acquisition_steps)]
    report_loss = [0. for _ in range(args.acquisition_steps)]
    start_epoch = start_iter = time.perf_counter()
    global_step = epoch * len(train_loader)

    epoch_targets = defaultdict(lambda: 0)
    epoch_target_counts = defaultdict(lambda: 0)

    for it, data in enumerate(train_loader):
        kspace, masked_kspace, mask, zf, gt, mean, std, _, _ = data
        # TODO: Maybe normalisation unnecessary for SSIM target?
        # shape after unsqueeze = batch x channel x columns x rows x complex
        kspace = kspace.unsqueeze(1).to(args.device)
        masked_kspace = masked_kspace.unsqueeze(1).to(args.device)
        mask = mask.unsqueeze(1).to(args.device)
        # shape after unsqueeze = batch x channel x columns x rows
        zf = zf.unsqueeze(1).to(args.device)
        gt = gt.unsqueeze(1).to(args.device)
        mean = mean.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
        std = std.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
        gt = gt * std + mean

        # Base reconstruction model forward pass
        recon_output = recon_model_forward_pass(args, recon_model, zf)

        # TODO: number of steps should be dependent on acceleration!
        #  But not all samples in batch have same acceleration: maybe change DataLoader at some point.
        at = time.perf_counter()

        for step in range(args.acquisition_steps):
            # recon_output.requires_grad = True
            # recon_output = Variable(recon_output, requires_grad=True)
            # Get impro model input and target: this step requires many forward passes through the reconstruction model
            # Thus in this Q training strategy, we only compute targets for a few rows, and train on those rows

            # activation = {}
            # def get_activation(name):
            #     def hook(model, input, output):
            #         activation[name] = output.detach()
            #     return hook
            # model.channel_layer.register_forward_hook(get_activation('first'))

            target, output, loss_mask = get_pred_and_target(args, kspace, masked_kspace, mask, gt, mean, std, model,
                                                            recon_model, recon_output, eps=eps, k=k)

            epoch_targets[step + 1] += (target * loss_mask).to('cpu').numpy().sum(axis=0)
            epoch_target_counts[step + 1] += loss_mask.to('cpu').numpy().sum(axis=0)

            # Compute loss and backpropagate
            # TODO: Do this every step?
            #  Update model before grabbing next row? Or wait for all steps to be done and then update together?
            #  Maybe use replay buffer type strategy here? Requires too much memory (need to save all gradients)?
            loss = F.l1_loss(output, target, reduction='none')  # TODO: Think about loss function
            # TODO: multiply by size of loss mask, to compensate for bias if not all slices get same number of targets
            loss = (loss * loss_mask).mean()
            # TODO: figure out why including loss mask suddenly helps?!

            # TODO: Karpathy bugfix trick: https://karpathy.github.io/2019/04/25/recipe/
            # loss = torch.sum(output[0, :])

            optimiser.zero_grad()
            loss.backward()

            # fig = plot_grad_flow(model.named_parameters())
            # fig.savefig(args.run_dir / 'gradient_flow_epoch{}_iter{}_step{}.png'.format(epoch, it, step))
            # assert ((recon_output.grad[i] == 0.).all() for i in range(1, 16) and (recon_output.grad[0] != 0).any())
            optimiser.step()

            epoch_loss[step] += loss.item() / len(train_loader)
            report_loss[step] += loss.item() / args.report_interval
            writer.add_scalar('TrainLoss_step{}'.format(step), loss.item(), global_step + it)

            # TODO: Sometimes the model will want to acquire a row that as already been acquired. This will in the
            #  next step lead to the model computing targets for the same situation (though maybe different k rows).
            #  This is good,
            #  because it will learn that the actual target for this row is 0, and thus will stop doing this.
            #  One potential issue is that, since no actual new row is acquired, this will keep happening until the
            #  model stops selecting the already sampled row, or until we've taken all our acquisition steps for this
            #  sample. This will bias the model training towards being aversive to sampling already sampled rows,
            #  which is intuitively a good thing, but keep this in mind as a potential explanation for extreme
            #  behaviour later.
            # Acquire row for next step: GREEDY
            pred_impro, next_rows = torch.max(output, dim=1)  # TODO: is greedy a good idea? Acquire multiple maybe?
            recon_output, zf, mean, std, mask, masked_kspace = acquire_row(kspace, masked_kspace, next_rows, mask,
                                                                           recon_model)

        if args.verbose >= 3:
            print('Time to train single batch of size {} for {} steps: {:.3f}'.format(
                args.batch_size, args.acquisition_steps, time.perf_counter() - at))

        if it % args.report_interval == 0:
            if it == 0:
                loss_str = ", ".join(["{}: {:.2f}".format(i + 1, args.report_interval * l * 1e3)
                                      for i, l in enumerate(report_loss)])
            else:
                loss_str = ", ".join(["{}: {:.2f}".format(i + 1, l * 1e3) for i, l in enumerate(report_loss)])
            logging.info(
                f'Epoch = [{epoch:3d}/{args.num_epochs:3d}], '
                f'Iter = [{it:4d}/{len(train_loader):4d}], '
                f'Time = {time.perf_counter() - start_iter:.2f}s, '
                f'Avg Loss per step (x1e3) = [{loss_str}] ',
            )
            report_loss = [0. for _ in range(args.acquisition_steps)]

        start_iter = time.perf_counter()

    for step in range(args.acquisition_steps):
        targets[epoch][step + 1] = epoch_targets[step + 1].tolist()
        target_counts[epoch][step + 1] = epoch_target_counts[step + 1].tolist()
    save_json(args.run_dir / 'targets_per_step_per_epoch.json', targets)
    save_json(args.run_dir / 'count_targets_per_step_per_epoch.json', target_counts)

    return np.mean(epoch_loss), time.perf_counter() - start_epoch


def acquire_row(kspace, masked_kspace, next_rows, mask, recon_model):
    zf, mean, std = acquire_new_zf_batch(kspace, masked_kspace, next_rows)
    # Don't forget to change mask for impro_model (necessary if impro model uses mask)
    # Also need to change masked kspace for recon model (getting correct next-step zf)
    # TODO: maybe do this in the acquire_new_zf_batch() function. Doesn't fit with other functions of same
    #  description, but this one is particularly used for this acquisition loop.
    for sl, next_row in enumerate(next_rows):
        mask[sl, :, :, next_row, :] = 1.
        masked_kspace[sl, :, :, next_row, :] = kspace[sl, :, :, next_row, :]
    # Get new reconstruction for batch
    recon_output = recon_model_forward_pass(args, recon_model, zf)
    return recon_output, zf, mean, std, mask, masked_kspace


def evaluate(args, epoch, recon_model, model, dev_loader, writer, k):
    """
    Evaluates using loss function: i.e. how close are the predicted improvements to the actual improvements.
    """
    model.eval()
    eps = 0  # Only evaluate for acquisitions the model actually wants to do
    epoch_loss = [0. for _ in range(args.acquisition_steps)]
    start = time.perf_counter()
    with torch.no_grad():
        for _, data in enumerate(dev_loader):
            kspace, masked_kspace, mask, zf, gt, mean, std, _, _ = data
            # TODO: Maybe normalisation unnecessary for SSIM target?
            # shape after unsqueeze = batch x channel x columns x rows x complex
            kspace = kspace.unsqueeze(1).to(args.device)
            masked_kspace = masked_kspace.unsqueeze(1).to(args.device)
            mask = mask.unsqueeze(1).to(args.device)
            # shape after unsqueeze = batch x channel x columns x rows
            zf = zf.unsqueeze(1).to(args.device)
            gt = gt.unsqueeze(1).to(args.device)
            mean = mean.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
            std = std.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
            gt = gt * std + mean

            # Base reconstruction model forward pass
            recon_output = recon_model_forward_pass(args, recon_model, zf)

            for step in range(args.acquisition_steps):
                # Get impro model input and target:
                # this step requires many forward passes through the reconstruction model
                target, output, loss_mask = get_pred_and_target(args, kspace, masked_kspace, mask, gt, mean, std, model,
                                                                recon_model, recon_output, eps, k)
                # Improvement model output
                loss = F.l1_loss(output, target, reduction='none')
                loss = (loss * loss_mask).mean()
                epoch_loss[step] += loss.item() / len(dev_loader)  # per batch

                # Greedy policy (size = batch)  # TODO: is this a good idea?
                pred_impro, next_rows = torch.max(output, dim=1)
                # Acquire this row
                zf, mean, std = acquire_new_zf_batch(kspace, masked_kspace, next_rows)
                # Don't forget to change mask for impro_model (necessary if impro model uses mask)
                for sl, next_row in enumerate(next_rows):
                    mask[sl, :, :, next_row, :] = 1.
                    masked_kspace[sl, :, :, next_row, :] = kspace[sl, :, :, next_row, :]
                # Get new reconstruction for batch
                recon_output = recon_model_forward_pass(args, recon_model, zf)

        for step, loss in enumerate(epoch_loss):
            writer.add_scalar('DevLoss_step{}'.format(step), loss, epoch)
    return np.mean(epoch_loss), time.perf_counter() - start


def evaluate_recons(args, epoch, recon_model, model, dev_loader, writer):
    """
    Evaluates using SSIM of reconstruction over trajectory. Doesn't require computing targets!
    """
    #     model.eval()
    ssims = 0
    c_ssims = 0
    # strategy: acquisition step: filename: [recons]
    recons = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    targets = defaultdict(list)
    metrics = {step: Metrics(METRIC_FUNCS) for step in range(args.acquisition_steps + 1)}
    c_metrics = {step: Metrics(METRIC_FUNCS) for step in range(args.acquisition_steps + 1)}

    epoch_outputs = defaultdict(list)

    start = time.perf_counter()
    with torch.no_grad():
        for it, data in enumerate(dev_loader):
            kspace, masked_kspace, mask, zf, gt, mean, std, fname, slices = data
            # shape after unsqueeze = batch x channel x columns x rows x complex
            kspace = kspace.unsqueeze(1).to(args.device)
            masked_kspace = masked_kspace.unsqueeze(1).to(args.device)
            mask = mask.unsqueeze(1).to(args.device)
            # shape after unsqueeze = batch x channel x columns x rows
            zf = zf.unsqueeze(1).to(args.device)
            gt = gt.unsqueeze(1).to(args.device)
            mean = mean.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
            std = std.unsqueeze(1).unsqueeze(2).unsqueeze(3).to(args.device)
            gt = gt * std + mean

            # Base reconstruction model forward pass
            recon_output = recon_model_forward_pass(args, recon_model, zf)

            c_masked_kspace = masked_kspace.clone()
            c_mask = mask.clone()
            norm_recon = recon_output[:, 0:1, :, :] * std + mean
            init_ssim_val = ssim(norm_recon, gt, size_average=False, data_range=1e-4).mean()

            batch_ssims = [init_ssim_val.item()]
            c_batch_ssims = []
            if epoch == -1:
                c_batch_ssims.append(init_ssim_val.item())

            eval_rec = norm_recon.to('cpu')
            for i in range(eval_rec.size(0)):
                recons[fname[i]]['center'][0].append(eval_rec[i].squeeze().numpy())
                recons[fname[i]]['improv'][0].append(eval_rec[i].squeeze().numpy())
                targets[fname[i]].append(gt.to('cpu')[i].squeeze().numpy())

            prev_acquired = [[] for _ in range(args.batch_size)]
            for step in range(args.acquisition_steps):
                # Improvement model output
                output = impro_model_forward_pass(args, model, recon_output, mask.squeeze(1).squeeze(1).squeeze(-1))
                epoch_outputs[step + 1].append(output.to('cpu').numpy())
                # Greedy policy (size = batch)
                # TODO: model often (at least initially) seems to want to choose rows that have already been chosen,
                #  which means it will never choose a new row again for this acquisition trajectory.
                # pred_impro, next_rows = torch.max(output, dim=1)

                # TODO: necessary? Seems so.
                _, topk_rows = torch.topk(output, args.acquisition_steps, dim=1)
                unacquired = (mask.squeeze(1).squeeze(1).squeeze(-1) == 0)
                next_rows = []
                for j, sl_topk_rows in enumerate(topk_rows):
                    for row in sl_topk_rows:
                        if row in unacquired[j, :].nonzero().flatten():
                            next_rows.append(row)
                            break
                # TODO: moving to device should happen only once before both loops. Should just mutate object after
                next_rows = torch.tensor(next_rows).long().to(args.device)

                if args.verbose >= 3:
                    print('Rows to acquire:', next_rows)
                if args.verbose >= 4:
                    for i, row in enumerate(next_rows):
                        row = row.item()
                        if row in prev_acquired[i]:
                            print(' - Batch {}, slice {}, step {}: selected row {} was previously acquired.'.format(
                                it, i, step, row))
                        else:
                            prev_acquired[i].append(row)

                # Acquire this row
                recon_output, zf, mean, std, mask, masked_kspace = acquire_row(kspace, masked_kspace, next_rows, mask,
                                                                               recon_model)
                norm_recon = recon_output[:, 0:1, :, :] * std + mean
                # shape = 1
                ssim_val = ssim(norm_recon, gt, size_average=False, data_range=1e-4).mean()
                # eventually shape = al_steps
                batch_ssims.append(ssim_val.item())

                eval_rec = norm_recon.to('cpu')
                for i in range(eval_rec.size(0)):
                    recons[fname[i]]['improv'][step + 1].append(eval_rec[i].squeeze().numpy())

                # Acquire center row
                if epoch == -1:
                    c_flat_unacq = (c_mask[0].squeeze() == 0).nonzero()
                    c_next_row = c_flat_unacq[len(c_flat_unacq) // 2]
                    c_next_rows = torch.tensor([c_next_row] * c_mask.size(0)).long().to(args.device)
                    c_recon_output, c_zf, c_mean, c_std, c_mask, c_masked_kspace = acquire_row(kspace, c_masked_kspace,
                                                                                               c_next_rows, c_mask,
                                                                                               recon_model)
                    c_norm_recon = c_recon_output[:, 0:1, :, :] * c_std + c_mean
                    # shape = 1
                    c_ssim_val = ssim(c_norm_recon, gt, size_average=False, data_range=1e-4).mean()
                    # eventually shape = al_steps
                    c_batch_ssims.append(c_ssim_val.item())

                    c_eval_rec = c_norm_recon.to('cpu')
                    for i in range(c_eval_rec.size(0)):
                        recons[fname[i]]['center'][step + 1].append(c_eval_rec[i].squeeze().numpy())

            # shape = al_steps
            ssims += np.array(batch_ssims) / len(dev_loader)
            c_ssims += np.array(c_batch_ssims) / len(dev_loader)

    for fname, vol_gts in targets.items():
        for step in range(args.acquisition_steps + 1):
            if epoch == -1:
                c_metrics[step].push(np.stack(vol_gts), np.stack(recons[fname]['center'][step]))
            metrics[step].push(np.stack(vol_gts), np.stack(recons[fname]['improv'][step]))

    print('\n')
    if epoch == -1:
        for step in range(args.acquisition_steps + 1):
            print('Center Metrics, step {}: {}'.format(step, c_metrics[step]))
    print('\n')
    for step in range(args.acquisition_steps + 1):
        print('Improv Metrics, step {}: {}'.format(step, metrics[step]))
    print('\n')

    for step in range(args.acquisition_steps):
        outputs[epoch][step + 1] = np.concatenate(epoch_outputs[step + 1], axis=0).tolist()
    save_json(args.run_dir / 'preds_per_step_per_epoch.json', outputs)

    for step, val in enumerate(ssims):
        writer.add_scalar('DevSSIM_step{}'.format(step), val, epoch)
    return ssims, c_ssims, time.perf_counter() - start


def visualise(args, epoch, model, display_loader, writer):
    # TODO: What to visualise here?
    pass


def main(args):
    args.trainQ = True
    # Reconstruction model
    recon_args, recon_model = load_recon_model(args)
    check_args_consistency(args, recon_args)

    # Improvement model to train
    if args.resume:
        checkpoint, model, optimiser = load_impro_model(args.checkpoint)
        args = checkpoint['args']
        best_dev_loss = checkpoint['best_dev_loss']
        start_epoch = checkpoint['epoch']
        del checkpoint
    else:
        model = build_impro_model(args)
        # Add mask parameters for training
        args = add_mask_params(args, recon_args)
        if args.data_parallel:
            model = torch.nn.DataParallel(model)
        optimiser = build_optim(args, model.parameters())
        best_dev_loss = 1e9
        start_epoch = 0
        # Create directory to store results in
        savestr = 'res{}_al{}_accel{}_{}_{}_k{}_{}'.format(args.resolution, args.acquisition_steps, args.accelerations,
                                                           args.impro_model_name, args.recon_model_name,
                                                           args.num_target_rows,
                                                           datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S"))
        args.run_dir = args.exp_dir / savestr
        args.run_dir.mkdir(parents=True, exist_ok=False)

    # Logging
    logging.info(args)
    logging.info(recon_model)
    logging.info(model)
    # Save arguments for bookkeeping
    args_dict = {key: str(value) for key, value in args.__dict__.items()
                 if not key.startswith('__') and not callable(key)}
    save_json(args.run_dir / 'args.json', args_dict)

    # Initialise summary writer
    writer = SummaryWriter(log_dir=args.run_dir / 'summary')

    # Parameter counting
    if args.verbose >= 1:
        print('Reconstruction model parameters: total {}, of which {} trainable and {} untrainable'.format(
            count_parameters(recon_model), count_trainable_parameters(recon_model),
            count_untrainable_parameters(recon_model)))
        print('Improvement model parameters: total {}, of which {} trainable and {} untrainable'.format(
            count_parameters(model), count_trainable_parameters(model), count_untrainable_parameters(model)))
    if args.verbose >= 3:
        for p in model.parameters():
            print(p.shape, p.numel())

    # Create data loaders
    train_loader, dev_loader, test_loader, display_loader = create_data_loaders(args)

    # TODO: remove this
    first_batch = next(iter(train_loader))
    train_loader = [first_batch] * 10
    dev_loader = [first_batch]

    scheduler = torch.optim.lr_scheduler.StepLR(optimiser, args.lr_step_size, args.lr_gamma)

    # Training and evaluation
    k = args.num_target_rows
    dev_ssims, c_dev_ssims, dev_ssim_time = evaluate_recons(args, -1, recon_model, model, dev_loader, writer)
    dev_ssims_str = ", ".join(["{}: {:.3f}".format(i, l) for i, l in enumerate(dev_ssims)])
    c_dev_ssims_str  = ", ".join(["{}: {:.3f}".format(i, l) for i, l in enumerate(c_dev_ssims)])
    logging.info(f'C_DevSSIM = [{c_dev_ssims_str}]')
    logging.info(f'  DevSSIM = [{dev_ssims_str}]')
    logging.info(f'DevSSIMTime = {dev_ssim_time:.2f}s')

    for epoch in range(start_epoch, args.num_epochs):
        scheduler.step(epoch)
        # eps decays a factor e after num_epochs / eps_decay_rate epochs: i.e. after 1/eps_decay_rate of all epochs
        # This way, the same factor decay happens after the same fraction of epochs, for various values for num_epochs
        # Examples for eps_decay_rate = 5:
        # E.g. if num_epochs = 10, we decay a factor e after 2 epochs: 1/5th of all epochs
        # E.g. if num_epochs = 50, we decay a factor e after 10 epochs: 1/5th of all epochs
        eps = np.exp(np.log(args.start_eps) - args.eps_decay_rate * epoch / args.num_epochs)
        train_loss, train_time = train_epoch(args, epoch, recon_model, model, train_loader, optimiser, writer, eps, k)
        # TODO: do both of these? Make more efficient?
        dev_loss, dev_loss_time = evaluate(args, epoch, recon_model, model, dev_loader, writer, k)
        dev_ssims, _, dev_ssim_time = evaluate_recons(args, epoch, recon_model, model, dev_loader, writer)
        # visualise(args, epoch, model, display_loader, writer)

        is_new_best = dev_loss < best_dev_loss
        best_dev_loss = min(best_dev_loss, dev_loss)
        save_model(args, args.run_dir, epoch, model, optimiser, best_dev_loss, is_new_best)

        dev_ssims_str = ", ".join(["{}: {:.3f}".format(i + 1, l) for i, l in enumerate(dev_ssims)])
        logging.info(
            f'Epoch = [{epoch:4d}/{args.num_epochs:4d}] TrainLoss = {train_loss:.3g} '
            f'DevLoss = {dev_loss:.3g} TrainTime = {train_time:.2f}s '
            f'DevLossTime = {dev_loss_time:.2f}s DevSSIMTime = {dev_ssim_time:.2f}s',
        )
        logging.info(f'DevSSIM = [{dev_ssims_str}]')
        # save_model(args, args.run_dir, epoch, model, optimiser, None, False)
    writer.close()


def create_arg_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument('--seed', default=42, type=int, help='Seed for random number generators')
    parser.add_argument('--resolution', default=320, type=int, help='Resolution of images')
    parser.add_argument('--dataset', choices=['fastmri', 'cifar10'], required=True,
                        help='Dataset to use.')

    # Data parameters
    parser.add_argument('--challenge', type=str, default='singlecoil',
                        help='Which challenge for fastMRI training.')
    parser.add_argument('--data-path', type=pathlib.Path, default=None,
                        help='Path to the dataset. Required for fastMRI training.')
    parser.add_argument('--sample-rate', type=float, default=1.,
                        help='Fraction of total volumes to include')
    parser.add_argument('--acquisition', type=str, default=None,
                        help='Use only volumes acquired using the provided acquisition method. Options are: '
                             'CORPD_FBK, CORPDFS_FBK (fat-suppressed), and not provided (both used).')

    # Reconstruction model
    parser.add_argument('--recon-model-checkpoint', type=pathlib.Path, required=True,
                        help='Path to a pretrained reconstruction model.')
    parser.add_argument('--recon-model-name', choices=['kengal_laplace', 'dist_gauss'], required=True,
                        help='Reconstruction model name corresponding to model checkpoint.')
    parser.add_argument('--use-recon-mask-params', action='store_true',
                        help='Whether to use mask parameter settings (acceleration and center fraction) that the '
                        'reconstruction model was trained on. This will overwrite any other mask settings.')

    parser.add_argument('--impro-model-name', choices=['convpool', 'convpoolmask', 'convbottle'], required=True,
                        help='Improvement model name (if using resume, must correspond to model at the '
                             'improvement model checkpoint.')
    # Mask parameters, preferably they match the parameters the reconstruction model was trained on. Also see
    # argument use-recon-mask-params above.
    parser.add_argument('--accelerations', nargs='+', default=[8], type=int,
                        help='Ratio of k-space columns to be sampled. If multiple values are '
                             'provided, then one of those is chosen uniformly at random for '
                             'each volume.')
    parser.add_argument('--reciprocals-in-center', nargs='+', default=[1], type=float,
                        help='Inverse fraction of rows (after subsampling) that should be in the center. E.g. if half '
                             'of the sampled rows should be in the center, this should be set to 2. All combinations '
                             'of acceleration and reciprocals-in-center will be used during training (every epoch a '
                             'volume randomly gets assigned an acceleration and center fraction.')

    parser.add_argument('--acquisition-steps', default=10, type=int, help='Acquisition steps to train for per image.')
    parser.add_argument('--num-pools', type=int, default=4, help='Number of ConvNet pooling layers. Note that setting '
                        'this too high will cause size mismatch errors, due to even-odd errors in calculation for '
                        'layer size post-flattening.')
    parser.add_argument('--of-which-four-pools', type=int, default=2, help='Number of of the num-pools pooling layers '
                        "that should 4x4 pool instead of 2x2 pool. E.g. if 2, first 2 layers will 4x4 pool, rest will "
                        "2x2 pool. Only used for 'pool' models.")
    parser.add_argument('--drop-prob', type=float, default=0, help='Dropout probability')
    parser.add_argument('--num-chans', type=int, default=32, help='Number of ConvNet channels')
    parser.add_argument('--in-chans', default=2, type=int, help='Number of image input channels'
                        'E.g. set to 2 if input is reconstruction and uncertainty map')
    parser.add_argument('--out-chans', type=int, default=32, help='Number of ConvNet output channels: these are input '
                        "for the FC layers that follow. Only used for 'bottle' models.")
    parser.add_argument('--fc-size', default=512, type=int, help='Size (width) of fully connected layer(s).')

    parser.add_argument('--batch-size', default=16, type=int, help='Mini batch size')
    parser.add_argument('--num-epochs', type=int, default=50, help='Number of training epochs')
    parser.add_argument('--lr', type=float, default=0.001, help='Learning rate')
    parser.add_argument('--lr-step-size', type=int, default=40,
                        help='Period of learning rate decay')
    parser.add_argument('--lr-gamma', type=float, default=0.1,
                        help='Multiplicative factor of learning rate decay')
    parser.add_argument('--weight-decay', type=float, default=0,
                        help='Strength of weight decay regularization. TODO: this currently breaks because many weights'
                        'are not updated every step (since we select certain targets only); FIX THIS.')

    parser.add_argument('--start-eps', type=float, default=0.5, help='Epsilon to start with. This determines '
                        'the trade-off between training on rows the improvement networks suggests, and training on '
                        'randomly sampled rows. Note that rows are selected mostly randomly at the start of training '
                        'regardless, since the initialised model will not have learned anything useful yet.')
    parser.add_argument('--eps-decay-rate', type=float, default=5, help='Epsilon decay rate. Epsilon decays a '
                        'factor e after a fraction 1/eps_decay_rate of all epochs have passed.')
    parser.add_argument('--num-target-rows', type=int, default=10, help='Number of rows to compute ground truth '
                        'targets for every update step.')

    parser.add_argument('--report-interval', type=int, default=100, help='Period of loss reporting')
    parser.add_argument('--data-parallel', action='store_true',
                        help='If set, use multiple GPUs using data parallelism')
    parser.add_argument('--num-workers', type=int, default=8, help='Number of workers to use for data loading')
    parser.add_argument('--device', type=str, default='cuda',
                        help='Which device to train on. Set to "cuda" to use the GPU')
    parser.add_argument('--exp-dir', type=pathlib.Path, required=True,
                        help='Directory where model and results should be saved. Will create a timestamped folder '
                        'in provided directory each run')

    parser.add_argument('--resume', action='store_true',
                        help='If set, resume the training from a previous model checkpoint. '
                             '"--checkpoint" should be set with this')
    parser.add_argument('--checkpoint', type=str,
                        help='Path to an existing checkpoint. Used along with "--resume"')

    parser.add_argument('--max-train-slices', type=int, default=None,
                        help='How many slices to train on maximally."')
    parser.add_argument('--max-dev-slices', type=int, default=None,
                        help='How many slices to evaluate on maximally."')
    parser.add_argument('--max-test-slices', type=int, default=None,
                        help='How many slices to test on maximally."')
    parser.add_argument('--verbose', type=int, default=1,
                        help='Set verbosity level. Lowest=0, highest=4."')
    return parser


if __name__ == '__main__':
    # To fix known issue with h5py + multiprocessing
    # See: https://discuss.pytorch.org/t/incorrect-data-using-h5py-with-dataloader/7079/2?u=ptrblck
    import torch.multiprocessing
    torch.multiprocessing.set_start_method('spawn')

    args = create_arg_parser().parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    main(args)