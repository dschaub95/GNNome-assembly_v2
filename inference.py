import argparse
import os
import pickle
import random
from tqdm import tqdm 

import torch
import torch.nn.functional as F
import dgl

from graph_dataset import AssemblyGraphDataset
from hyperparameters import get_hyperparameters
import models
import algorithms
from utils import load_graph_data


def predict_new(model, graph, succs, preds, edges, device):
    x = graph.ndata['x'].to(device)
    e = graph.edata['e'].to(device)
    edge_logits = model(graph, x, e)
    # TODO: Problem, my block model doesn't work on full graphs!
    # TODO: I can still iterate over the batches and append the predictions
    edge_logits= edge_logits.squeeze(-1)
    edge_p = F.sigmoid(edge_logits)
    walks = decode_new(graph, edge_p, succs, preds, edges)
    return walks
    # or (later) translate walks into sequences
    # what with the sequences? Store into FASTA ofc


def decode_new(graph, edges_p, neighbors, predecessors, edges):
    # Choose starting node for the first time
    walks = []
    visited = set()
    # ----- Modify this later ------
    all_nodes = {n.item() for n in graph.nodes()}
    correct_nodes = {n for n in range(graph.num_nodes()) if graph.ndata['y'][n] == 1}
    potential_nodes = correct_nodes
    # ------------------------------
    while True:
        potential_nodes = potential_nodes - visited
        start = get_random_start(potential_nodes)
        if start is None:
            break
        visited.add(start)
        visited.add(start ^ 1)
        walk_f, visited_f = walk_forwards(start, edges_p, neighbors, edges, visited)
        walk_b, visited_b = walk_backwards(start, edges_p, predecessors, edges, visited)
        walk = walk_b[:-1] + [start] + walk_f[1:]
        visited = visited | visited_f | visited_b
        walks.append(walk)
    walks = sorted(walks, key=lambda x: len(x))
    return walks
    

def get_random_start(potential_nodes, nodes_p=None):
    # potential_nodes = {n.item() for n in graph.nodes()}
    if len(potential_nodes) < 10:
        return None
    potential_nodes = potential_nodes
    start = random.sample(potential_nodes, 1)[0]
    # start = max(potential_nodes_p)
    return start


def walk_forwards(start, edges_p, neighbors, edges, visited):
    current = start
    walk = []
    while True:
        if current in visited and walk:
            break
        walk.append(current)
        visited.add(current)
        visited.add(current ^ 1)
        if len(neighbors[current]) == 0:
            break 
        if len(neighbors[current]) == 1:
            current = neighbors[current][0]
            continue
        neighbor_edges = [edges[current, n] for n in neighbors[current] if n not in visited]
        if not neighbor_edges:
            break
        neighbor_p = edges_p[neighbor_edges]
        _, index = torch.topk(neighbor_p, k=1, dim=0)
        choice = neighbors[current][index]
        current = choice
    return walk, visited


def walk_backwards(start, edges_p, predecessors, edges, visited):
    current = start
    walk = []
    while True:
        if current in visited and walk:
            break
        walk.append(current)
        visited.add(current)
        visited.add(current ^ 1)
        if len(predecessors[current]) == 0:
            break 
        if len(predecessors[current]) == 1:
            current = predecessors[current][0]
            continue
        neighbor_edges = [edges[n, current] for n in predecessors[current] if n not in visited]
        if not neighbor_edges:
            break
        neighbor_p = edges_p[neighbor_edges]
        _, index = torch.topk(neighbor_p, k=1, dim=0)
        choice = predecessors[current][index]
        current = choice
    walk = list(reversed(walk))
    return walk, visited


def predict_old(model, graph, pred, neighbors, reads, edges):
    starts = [k for k,v in pred.items() if len(v)==0 and graph.ndata['read_strand'][k]==1]
    
    components = algorithms.get_components(graph, neighbors, pred)
    # components = [c for c in components if len(c) >= 10]  # For some reason components are not split properly so I should leave this line out
    components = sorted(components, key=lambda x: -len(x))
    walks = []

    logits = model(graph, reads)

    for i, component in enumerate(components):
        try:
            start_nodes = [node for node in component if len(pred[node]) == 0 and graph.ndata['read_strand'][node] == 1]
            start = min(start_nodes, key=lambda x: graph.ndata['read_start'][x])  # TODO: Wait a sec, 'read_start' shouldn't be used!!
            walk = decode_old(neighbors, edges, start, logits)
            walks.append(walk)
        except ValueError:
            # Negative strand
            # TODO: Solve later
            pass

    walks = sorted(walks, key=lambda x: -len(x))
    final = [walks[0]]

    if len(walks) > 1:
        all_nodes = set(walks[0])
        for w in walks[1:]:
            if len(w) < 10:
                continue
            if len(set(w) & all_nodes) == 0:
                final.append(w)
                all_nodes = all_nodes | set(w)

    return final


def decode_old(neighbors, edges, start, logits):
    current = start
    visited = set()
    walk = []

    while True:
        if current in visited:
            break
        walk.append(current)
        visited.add(current)
        visited.add(current ^ 1)
        if len(neighbors[current]) == 0:
            break
        if len(neighbors[current]) == 1:
            current = neighbors[current][0]
            continue

        neighbor_edges = [edges[current, n] for n in neighbors[current]]
        neighbor_logits = logits.squeeze(1)[neighbor_edges]

        _, index = torch.topk(neighbor_logits, k=1, dim=0)
        choice = neighbors[current][index]
        current = choice

    return walk


def calculate_N50(list_of_lengths):
    """Calculate N50 for a sequence of numbers.
    Args:
        list_of_lengths (list): List of numbers.
    Returns:
        float: N50 value.
    """
    tmp = []
    for tmp_number in set(list_of_lengths):
        tmp += [tmp_number] * list_of_lengths.count(tmp_number) * tmp_number
    tmp.sort()

    if (len(tmp) % 2) == 0:
        median = (tmp[int(len(tmp) / 2) - 1] + tmp[int(len(tmp) / 2)]) / 2
    else:
        median = tmp[int(len(tmp) / 2)]

    return median

def calculate_NG50(list_of_lengths, ref_length):
    """Calculate N50 for a sequence of numbers.
    Args:
        list_of_lengths (list): List of numbers.
    Returns:
        float: N50 value.
    """
    if ref_length == 0:
        return -1
    list_of_lengths.sort(reverse=True)
    total_bps = 0
    for contig in list_of_lengths:
        total_bps += contig
        if total_bps > ref_length/2:
            return contig
    return -1

def txt_output(f, txt):
    print(f'\t{txt}')
    f.write(f'\t{txt}\n')

def analyze(graph, gnn_paths, greedy_paths, out, ref_length):
    with open(f'{out}/analysis.txt', 'w') as f:
        # f.write(f'Chromosome total length:\t\n')
        #print(out.split("/"), out.split("/")[-2])
        gnn_contig_lengths = []
        for path in gnn_paths:
            contig_len = graph.ndata["read_end"][path[-1]] - graph.ndata["read_start"][path[0]]
            gnn_contig_lengths.append(abs(contig_len).item())
        txt_output(f, 'GNN: ')
        txt_output(f, f'Contigs: \t{gnn_contig_lengths}')
        txt_output(f,f'Contigs amount:\t{len(gnn_contig_lengths)}')
        txt_output(f,f'Longest Contig:\t{max(gnn_contig_lengths)}')
        txt_output(f,f'Reconstructed:\t{sum(gnn_contig_lengths)}')
        txt_output(f,f'Percentage:\t{sum(gnn_contig_lengths)/ref_length*100}')
        n50_gnn = calculate_N50(gnn_contig_lengths)
        txt_output(f,f'N50:\t{n50_gnn}')
        ng50_gnn = calculate_NG50(gnn_contig_lengths, ref_length)
        txt_output(f,f'NG50:\t{ng50_gnn}')


        txt_output(f,f'Greedy paths:\t{len(greedy_paths)}\n')
        greedy_contig_lengths = []
        for path in greedy_paths:
            contig_len = graph.ndata["read_end"][path[-1]] - graph.ndata["read_start"][path[0]]
            greedy_contig_lengths.append(abs(contig_len).item())
        txt_output(f, 'Greedy: ')
        txt_output(f, f'Contigs: \t{greedy_contig_lengths}')
        txt_output(f,f'Contigs amount:\t{len(greedy_contig_lengths)}')
        txt_output(f,f'Longest Contig:\t{max(greedy_contig_lengths)}')
        txt_output(f,f'Reconstructed:\t{sum(greedy_contig_lengths)}')
        txt_output(f,f'Percentage:\t{sum(greedy_contig_lengths)/ref_length*100}')
        n50_greedy = calculate_N50(greedy_contig_lengths)
        txt_output(f,f'N50:\t{n50_greedy}')
        ng50_greedy = calculate_NG50(greedy_contig_lengths, ref_length)
        txt_output(f,f'NG50:\t{ng50_greedy}')



def test_walk(data_path, model_path,  device):
    hyperparameters = get_hyperparameters()
    # device = hyperparameters['device']
    dim_latent = hyperparameters['dim_latent']
    num_gnn_layers = hyperparameters['num_gnn_layers']
    # use_reads = hyperparameters['use_reads']

    # node_dim = hyperparameters['node_features']
    # edge_dim = hyperparameters['edge_dim']

    # if model_path is None:
    #     model_path = 'pretrained/model_32d_8l.pt'  # Best performing model
    model = models.BlockGatedGCNModel(1, 2, 128, 4).to(device)
    model.load_state_dict(torch.load(model_path, map_location=torch.device(device)))
    # model.eval()

    ds = AssemblyGraphDataset(data_path)

    # info_all = load_graph_data(len(ds), data_path, False)

    idx, g = ds[0]
    sampler = dgl.dataloading.MultiLayerFullNeighborSampler(4)
    graph_ids = torch.arange(g.num_edges()).int()
    dl = dgl.dataloading.EdgeDataLoader(g, graph_ids, sampler, batch_size=4096*10, shuffle=False, drop_last=False)
    logits = torch.tensor([]).to(device)
    with torch.no_grad():
        for input_nodes, edge_subgraph, blocks in tqdm(dl):
            blocks = [b.to(device) for b in blocks]
            edge_subgraph = edge_subgraph.to(device)
            x = blocks[0].srcdata['x']
            e_0 = blocks[0].edata['e']
            e_subgraph = edge_subgraph.edata['e']
            # print(x.squeeze(-1))
            # print(e_0)
            # print(e_subgraph)
            p = model(edge_subgraph, blocks, x, e_0, e_subgraph).squeeze(-1)
            # print(p)
            # print(p.sum())
            logits = torch.cat((logits, p), dim=0)
    return logits



def inference(model_path=None, data_path=None):
    hyperparameters = get_hyperparameters()
    device = hyperparameters['device']
    dim_latent = hyperparameters['dim_latent']
    num_gnn_layers = hyperparameters['num_gnn_layers']
    use_reads = hyperparameters['use_reads']

    node_dim = hyperparameters['node_features']
    edge_dim = hyperparameters['edge_dim']

    # if model_path is None:
    #     model_path = 'pretrained/model_32d_8l.pt'  # Best performing model
    model = models.BlockGatedGCNModel(node_dim, edge_dim, dim_latent, num_gnn_layers).to(device)
    model.load_state_dict(torch.load(model_path, map_location=torch.device(device)))
    model.eval()

    if data_path is None:
        data_path = 'data/train'
    ds = AssemblyGraphDataset(data_path)

    info_all = load_graph_data(len(ds), data_path, use_reads)

    for i in range(len(ds)):
        idx, graph = ds[i]
        print(f'Graph index: {idx}')
        graph = graph.to(device)
        
        succ = info_all['succs'][idx]
        pred = info_all['preds'][idx]
        if use_reads:
            reads = info_all['reads'][idx]
        else:
            reads = None
        edges = info_all['edges'][idx]

        walks = predict_new(model, graph, pred, succ, reads, edges, device)

        inference_path = os.path.join(data_path, 'inference')
        if not os.path.isdir(inference_path):
            os.mkdir(inference_path)
        pickle.dump(walks, open(f'{inference_path}/{idx}_predict.pkl', 'wb'))

        start_nodes = [w[0] for w in walks]

        # TODO: Greedy will not be too relevant soon, most likely
        baselines = []
        for start in start_nodes:
            baseline = algorithms.greedy(graph, start, succ, pred, edges)
            baselines.append(baseline)
        pickle.dump(baselines, open(f'{inference_path}/{idx}_greedy.pkl', 'wb'))

        analyze(graph, walks, baselines, inference_path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default=None)
    parser.add_argument('--data', default=None)
    args = parser.parse_args()
    model_path = args.model
    data_path = args.data
    inference(model_path, data_path)
