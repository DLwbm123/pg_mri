import logging
import time
import datetime
import random
import argparse
import pathlib

import numpy as np
import torch
import torch.nn.functional as F
from tensorboardX import SummaryWriter

# Importing Arguments is required for loading of Gauss reconstruction model
from src.recon_models.unet_dist_model import Arguments

from src.helpers.metrics import ssim
from src.helpers.utils import (add_mask_params, save_json, check_args_consistency, count_parameters,
                               count_trainable_parameters, count_untrainable_parameters)
from src.helpers.data_loading import create_data_loaders
from src.recon_models.recon_model_utils import (acquire_new_zf_exp_batch, acquire_new_zf_batch,
                                                recon_model_forward_pass, load_recon_model)
from src.impro_models.impro_model_utils import (load_impro_model, build_impro_model, build_optim, save_model,
                                                impro_model_forward_pass)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_batch_target(args, kspace, masked_kspace, mask, gt, mean, std, model, recon_model, recon_output,
                        eps=.1, k=10):
    recon = recon_output[:, 0:1, ...]  # Other channels are uncertainty maps + other input to the impro model
    norm_recon = recon * std + mean  # Back to original scale for metric  # TODO: is this unnormalisation necessary?

    # shape = batch
    # base_score = F.mse_loss(norm_recon, gt, reduction='none').mean(-1).mean(-1)
    base_score = ssim(norm_recon, gt, size_average=False, data_range=1e-4).mean(-1).mean(-1)  # keep channel dim = 1

    # Rows than can be acquired
    unacquired = (mask.squeeze() == 0)
    # Create improvement targets for this batch
    target = torch.zeros((mask.size(0), mask.size(-2))).to(args.device)  # batch_size x resolution
    # Impro model output (Q(a|s) is used to select which targets to actually compute for fine-tuning Q(a|s)
    output = impro_model_forward_pass(args, model, recon_output, mask.squeeze())

    # shape = batch x k
    _, topk_inds = torch.topk(output, k, dim=1)  # TODO: filter on unacquired?

    # Initialise output to train for: this tensor will be filled with some of the impro model outputs, such that we
    # only train for those outputs, and ignore the rest.
    # Another way to do this would be to use the full output, and have the target be equal to output except for a
    # select few. This will lead to gradients of 0, but will still try to backpropagate those gradients, resulting
    # in more computation.
    output_train = torch.zeros_like(output)

    # Now set random rows to overwrite topk values and inds (epsilon greedily)
    for i in range(mask.size(0)):
        # Set topk values in output
        sl_topk_inds = topk_inds[i, :]
        output_train[i, sl_topk_inds] = output[i, sl_topk_inds]
        # Some of these will be replaced by random rows: select which ones to replace
        topk_replace = (eps > torch.rand(k))  # which of the top k rows to replace with random rows (binary mask)
        topk_replace_inds = topk_replace.nonzero().flatten()  # which of the top k rows to replace with random rows
        replace_inds = sl_topk_inds[topk_replace_inds]  # which output rows to replace with random rows
        # Which random rows to pick to replace the some of the topk rows (sampled without replacement here)
        # TODO: with replacement instead?
        sl_unacq = unacquired[i, :].nonzero().flatten()
        sl_unacq_shuffle = sl_unacq[torch.randperm(len(sl_unacq))]  # Shuffle potential rows to acquire for this slice
        randrow_inds = sl_unacq_shuffle[:topk_replace.sum()]  # Pick the first n, where n is the number to replace
        # Set training output for random rows (we want to train these rows, replacing a few of the topk rows)
        output_train[i, randrow_inds] = output[i, randrow_inds]
        # Replace training output for nongreedy rows (we don't want to train these rows this iteration)
        output_train[i, replace_inds] = 0
        # Replace indices similarly, so that we know which rows to compute true targets for
        topk_inds[i, topk_replace_inds] = randrow_inds

    # shape = batch x rows = k x res x res
    zf_exp, mean_exp, std_exp = acquire_new_zf_exp_batch(kspace, masked_kspace, topk_inds)
    # shape = batch . rows x 1 x res x res
    zf_input = zf_exp.view(mask.size(0) * k, 1, mask.size(-2), mask.size(-2))
    # shape = batch . rows x 2 x res x res
    recons_output = recon_model_forward_pass(args, recon_model, zf_input)
    # shape = batch . rows x 1 x res x res
    recons = recons_output[:, 0:1, ...]
    # shape = batch x rows x res x res
    recons = recons.view(mask.size(0), k, mask.size(-2), mask.size(-2))
    norm_recons = recons * std_exp + mean_exp  # TODO: Normalisation necessary?
    gt = gt.expand(-1, k, -1, -1)
    # scores = batch x rows (channels), base_score = batch x 1
    scores = ssim(norm_recons, gt, size_average=False, data_range=1e-4).mean(-1).mean(-1)
    impros = scores - base_score
    # scores = F.mse_loss(norm_recons, gt, reduction='none').mean(-1).mean(-1)
    # impros = (base_score.unsqueeze(1) - scores) * 1e12
    # target = batch x rows, topk_inds and impros = batch x k
    for j, sl_topk_inds in enumerate(topk_inds):
        target[j, sl_topk_inds] = impros[j, :]
    return target


def train_epoch(args, epoch, recon_model, model, train_loader, optimiser, writer, eps):
    # TODO: try batchnorm in FC layers
    model.train()
    avg_loss = [0. for _ in range(args.acquisition_steps)]
    start_epoch = start_iter = time.perf_counter()
    global_step = epoch * len(train_loader)
    for iter, data in enumerate(train_loader):
        # TODO: Do we train one step per sample, or take multiple steps?
        #  Will this work with batching, or do we need to do one sample at a time?
        #  It seems we need to take multiple steps, as evaluation shows the model wants to select the same row
        #  over and over, even if it has already sampled it (it has never seen the other cases where the improvement
        #  is 0 for that row). Probably we'll need to use the bandit-like model to do this in a computationally
        #  feasible way.
        kspace, masked_kspace, mask, zf, gt, mean, std, _ = data
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

        # TODO: train over multiple acquisition steps
        # Base reconstruction model forward pass
        recon_output = recon_model_forward_pass(args, recon_model, zf)

        # TODO: number of steps should be dependent on acceleration!
        #  But not all samples in batch have same acceleration: maybe change DataLoader at some point.
        at = time.perf_counter()
        for step in range(args.acquisition_steps):
            # Get impro model input and target: this step requires many forward passes through the reconstruction model
            target = create_batch_target(args, kspace, masked_kspace, mask, gt, mean, std, model,
                                         recon_model, recon_output, eps=eps)
            # Improvement model output
            output = impro_model_forward_pass(args, model, recon_output, mask.squeeze())

            # Compute loss and backpropagate
            # TODO: Do this every step?
            #  Update model before grabbing next row? Or wait for all steps to be done and then update together?
            #  Maybe use replay buffer type strategy here? Requires too much memory (need to save all gradients)?
            loss = F.l1_loss(output, target)  # TODO: Think about loss function
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
            avg_loss[step] = 0.99 * avg_loss[step] + 0.01 * loss.item() if iter > 0 else loss.item()
            writer.add_scalar('TrainLoss_step{}'.format(step), loss.item(), global_step + iter)

            # TODO: Sometimes the model will want to acquire a row that as already been acquired. This is good,
            #  because it will learn that the actual target for this row is 0, and thus will stop doing this.
            #  One potential issue is that, since no actual new row is acquired, this will keep happening until the
            #  model stops selecting the already sampled row, or until we've taken all our acquisition steps for this
            #  sample. This will bias the model training towards being aversive to sampling already sampled rows,
            #  which is intuitively a good thing, but keep this in mind as a potential explanation for extreme
            #  behaviour later.
            # Acquire row for next step
            pred_impro, next_rows = torch.max(output, dim=1)
            zf, mean, std = acquire_new_zf_batch(kspace, masked_kspace, next_rows)
            # Don't forget to change mask for impro_model (necessary if impro model uses mask)
            for sl, next_row in enumerate(next_rows):
                mask[sl, :, :, next_row, :] = 1.

            # Get new reconstruction for batch
            recon_output = recon_model_forward_pass(args, recon_model, zf)

        if args.verbose >= 3:
            print('Time to train single batch of size {} for {} steps: {:.3f}'.format(
                args.batch_size, args.acquisition_steps, time.perf_counter() - at))

        if iter % args.report_interval == 0:
            logging.info(
                f'Epoch = [{epoch:3d}/{args.num_epochs:3d}] '
                f'Iter = [{iter:4d}/{len(train_loader):4d}] '
                f'Avg Loss = {np.mean(avg_loss).item():.4g} '
                f'Time = {time.perf_counter() - start_iter:.4f}s',
            )
        start_iter = time.perf_counter()
    return avg_loss, time.perf_counter() - start_epoch


def evaluate(args, epoch, recon_model, model, dev_loader, writer):
    # TODO: implement (maybe same as for non Q training?)
    pass


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
        args.run_dir = args.exp_dir / datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
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
    scheduler = torch.optim.lr_scheduler.StepLR(optimiser, args.lr_step_size, args.lr_gamma)

    # Training and evaluation
    for epoch in range(start_epoch, args.num_epochs):
        scheduler.step(epoch)
        # eps decays a factor e after num_epochs / eps_decay_rate epochs: i.e. after 1/eps_decay_rate of all epochs
        # This way, the same factor decay happens after the same fraction of epochs, for various values for num_epochs
        # E.g. if num_epochs = 10, we decay a factor e after 2 epochs: 1/5th of all epochs
        # E.g. if num_epochs = 50, we decay a factor e after 10 epochs: 1/5th of all epochs
        eps = np.exp(np.log(args.start_eps) - args.eps_decay_rate * epoch / args.num_epochs)
        train_loss, train_time = train_epoch(args, epoch, recon_model, model, train_loader, optimiser, writer, eps)

        # dev_loss, dev_time = evaluate(args, epoch, recon_model, model, dev_loader, writer)
        # # visualise(args, epoch, model, display_loader, writer)
        #
        # is_new_best = dev_loss < best_dev_loss
        # best_dev_loss = min(best_dev_loss, dev_loss)
        # save_model(args, args.run_dir, epoch, model, optimiser, best_dev_loss, is_new_best)
        # logging.info(
        #     f'Epoch = [{epoch:4d}/{args.num_epochs:4d}] TrainLoss = {train_loss:.4g} '
        #     f'DevLoss = {dev_loss:.4g} TrainTime = {train_time:.4f}s DevTime = {dev_time:.4f}s',
        # )
        save_model(args, args.run_dir, epoch, model, optimiser, None, False)
    writer.close()


def create_arg_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument('--seed', default=42, type=int, help='Seed for random number generators')
    parser.add_argument('--resolution', default=320, type=int, help='Resolution of images')

    # Data parameters
    parser.add_argument('--challenge', type=str, default='singlecoil',
                        help='Which challenge')
    parser.add_argument('--data-path', type=pathlib.Path, required=True,
                        help='Path to the dataset')
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
    parser.add_argument('--accelerations', nargs='+', default=[4, 6, 8, 10], type=int,
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

    parser.add_argument('--batch-size', default=16, type=int, help='Mini batch size')
    parser.add_argument('--krow-batch-size', default=64, type=int, help='Batch size for target creation loop')
    parser.add_argument('--num-epochs', type=int, default=50, help='Number of training epochs')
    parser.add_argument('--lr', type=float, default=0.001, help='Learning rate')
    parser.add_argument('--lr-step-size', type=int, default=40,
                        help='Period of learning rate decay')
    parser.add_argument('--lr-gamma', type=float, default=0.1,
                        help='Multiplicative factor of learning rate decay')
    parser.add_argument('--weight-decay', type=float, default=0,
                        help='Strength of weight decay regularization')

    parser.add_argument('--start-eps', type=float, default=0.001, help='Epsilon to start with. This determines '
                        'the trade-off between training on rows the improvement networks suggests, and training on '
                        'randomly sampled rows.')
    parser.add_argument('--eps-decay-rate', type=float, default=0.001, help='Epsilon decay rate. Epsilon decays a '
                        'factor e after a fraction 1/eps_decay_rate of all epochs have passed.')

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
                        help='Set verbosity level. Lowest=0, highest=3."')
    return parser


if __name__ == '__main__':
    args = create_arg_parser().parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    main(args)
