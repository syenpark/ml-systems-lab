"""DDP checkpoint / resume: crash mid-run, resume, and get the same weights.

    # 1) uninterrupted run (reference)
    torchrun --nproc_per_node=2 --master_addr=127.0.0.1 --master_port=29500 c1_checkpoint.py --epochs 4 --ckpt /tmp/ckpt.pt --fresh
    # 2) same run, but every rank "dies" in the middle of epoch 2
    torchrun --nproc_per_node=2 --master_addr=127.0.0.1 --master_port=29500 c1_checkpoint.py --epochs 4 --ckpt /tmp/ckpt.pt --fresh --crash-at-epoch 2
    # 3) resume from the last checkpoint
    torchrun --nproc_per_node=2 --master_addr=127.0.0.1 --master_port=29500 c1_checkpoint.py --epochs 4 --ckpt /tmp/ckpt.pt --resume
    # (optional) crash again with 2), then resume WITHOUT the optimizer state → different final weights
    ... --resume --no-opt-state
"""

from __future__ import annotations

import argparse
import os

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, TensorDataset
from torch.utils.data.distributed import DistributedSampler


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--ckpt", default="/tmp/ckpt.pt")
    p.add_argument("--fresh", action="store_true", help="delete an old checkpoint first")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--crash-at-epoch", type=int, default=-1)
    p.add_argument("--no-opt-state", action="store_true", help="resume without optimizer state")
    return p.parse_args()


def make_data() -> TensorDataset:
    g = torch.Generator().manual_seed(0)
    x = torch.randn(512, 8, generator=g)
    y = x @ torch.arange(1.0, 9.0).unsqueeze(1) + 0.1 * torch.randn(512, 1, generator=g)
    return TensorDataset(x, y)


def checksum(model: torch.nn.Module) -> float:
    return sum(p.detach().double().sum().item() for p in model.parameters())


def main() -> None:
    args = parse_args()
    dist.init_process_group("gloo")  # CPU-friendly; use "nccl" on GPUs
    rank, world = dist.get_rank(), dist.get_world_size()

    if args.fresh and rank == 0 and os.path.exists(args.ckpt):
        os.remove(args.ckpt)
    dist.barrier()

    torch.manual_seed(0)
    model = torch.nn.Linear(8, 1)
    ddp = DDP(model)  # broadcasts rank 0's weights to every rank
    opt = torch.optim.SGD(ddp.parameters(), lr=0.01, momentum=0.9)  # momentum = optimizer STATE

    ds = make_data()
    sampler = DistributedSampler(ds, num_replicas=world, rank=rank, shuffle=True, seed=0)
    loader = DataLoader(ds, batch_size=32, sampler=sampler)

    start_epoch = 0
    if args.resume:
        ckpt = torch.load(args.ckpt, map_location="cpu")  # every rank reads the same file
        ddp.module.load_state_dict(ckpt["model"])  # .module = the model inside the DDP wrapper
        if not args.no_opt_state:
            opt.load_state_dict(ckpt["optimizer"])
        torch.set_rng_state(ckpt["rng"])
        start_epoch = ckpt["epoch"] + 1  # continue AFTER the last finished epoch
        if rank == 0:
            print(f"[resume] loaded epoch {ckpt['epoch']} → starting at epoch {start_epoch}"
                  f"{' (optimizer state NOT loaded)' if args.no_opt_state else ''}")

    for epoch in range(start_epoch, args.epochs):
        sampler.set_epoch(epoch)  # same shuffle as an uninterrupted run, because epoch was saved
        for step, (xb, yb) in enumerate(loader):
            if epoch == args.crash_at_epoch and step == len(loader) // 2:
                if rank == 0:
                    print(f"[crash] epoch {epoch}, step {step}: all ranks die (work since the last checkpoint is lost)")
                os._exit(1)
            loss = torch.nn.functional.mse_loss(ddp(xb), yb)
            opt.zero_grad()
            loss.backward()
            opt.step()

        if rank == 0:  # only ONE rank writes (all ranks hold identical weights)
            state = {"model": ddp.module.state_dict(), "optimizer": opt.state_dict(),
                     "epoch": epoch, "rng": torch.get_rng_state()}
            tmp = args.ckpt + ".tmp"
            torch.save(state, tmp)
            os.replace(tmp, args.ckpt)  # atomic: a crash mid-save never leaves a broken file
            print(f"epoch {epoch}: loss {loss.item():.4f} · saved checkpoint · weights checksum {checksum(ddp.module):.6f}")
        dist.barrier()  # nobody starts the next epoch before the file exists

    if rank == 0:
        print(f"FINAL weights checksum {checksum(ddp.module):.6f}")
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
