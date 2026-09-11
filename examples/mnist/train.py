"""Small, real MNIST training workload for validating CCI → image → ACP → AFS."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from torchvision.datasets import MNIST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--epochs', type=int, default=2)
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    if args.epochs < 1 or args.threads < 1 or not args.prepare_only and args.output_dir is None:
        parser.error('epochs/threads must be positive and training requires --output-dir')
    torch.set_num_threads(args.threads)
    torch.manual_seed(2026)
    # ACP training requires pre-staged data and therefore does not need internet.
    train = MNIST(args.data_dir, train=True, download=args.prepare_only)
    test = MNIST(args.data_dir, train=False, download=args.prepare_only)
    if args.prepare_only:
        print(json.dumps({'prepared': True, 'train_samples': len(train), 'test_samples': len(test)}), flush=True)
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if any(args.output_dir.iterdir()):
        raise RuntimeError('Output directory must be empty; use a new run directory')
    start = time.monotonic()
    log = (args.output_dir / 'metrics.jsonl').open('x', encoding='utf-8')

    def emit(event, **values):
        row = {'event': event, **values}
        line = json.dumps(row)
        print(line, flush=True)
        log.write(line + '\n')
        log.flush()

    def tensors(dataset):
        return TensorDataset(dataset.data.float().div_(255).flatten(1), dataset.targets)

    loader = DataLoader(tensors(train), batch_size=256, shuffle=True, num_workers=0)
    test_loader = DataLoader(tensors(test), batch_size=512, num_workers=0)
    model = nn.Sequential(nn.Linear(784, 128), nn.ReLU(), nn.Linear(128, 10))
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    criterion = nn.CrossEntropyLoss()

    @torch.no_grad()
    def evaluate():
        model.eval()
        correct, loss, count = 0, 0.0, 0
        for images, labels in test_loader:
            logits = model(images)
            correct += int((logits.argmax(1) == labels).sum())
            loss += float(criterion(logits, labels)) * len(labels)
            count += len(labels)
        return {'test_accuracy': correct / count, 'test_loss': loss / count}

    baseline = evaluate()
    emit('baseline', **baseline)
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss, count = 0.0, 0
        for images, labels in loader:
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(images), labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item() * len(labels)
            count += len(labels)
        metrics = evaluate()
        emit('epoch', epoch=epoch, train_loss=total_loss / count, **metrics)
    torch.save(model.state_dict(), args.output_dir / 'model.pt')
    result = dict(status='completed', device='cpu', epochs=args.epochs, train_samples=len(train),
                  test_samples=len(test), seed=2026, torch_version=torch.__version__,
                  elapsed_seconds=round(time.monotonic() - start, 3), baseline=baseline, **metrics,
                  code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  checkpoint_sha256=hashlib.sha256((args.output_dir / 'model.pt').read_bytes()).hexdigest())
    (args.output_dir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    emit('completed', **result)
    log.close()


if __name__ == '__main__':
    main()
