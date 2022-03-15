import torch


def get_hyperparameters():
    return {
        'seed': 0,
        'num_epochs': 10000,
        'dim_latent': 128,
        'num_gnn_layers': 3,
        'batch_size': 1,
        'patience_limit': 10,
        'device': 'cuda:7' if torch.cuda.is_available() else 'cpu',
        'lr': 1e-3,
        'walk_length': 10,
        'bias': False,
        'gnn_mode': 'builtin',
        'encode': 'none',
        'norm': 'all',
        'use_reads': False,
        'use_amp': True,
    }
