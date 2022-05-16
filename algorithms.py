import os
import math
import pickle
import subprocess
from collections import deque
from datetime import datetime

import dgl
import torch

 

def greedy_message_func(edges):
    s_ji = edges.data['score'] # j=>i or src=>dst
    idx_i = edges.dst['idx_nodes']   # i or dst
    idx_j = edges.src['idx_nodes']   # j or src
    return {'s_ji': s_ji, 'idx_i': idx_i, 'idx_j': idx_j}


def greedy_reduce_func(nodes):
    s_ji = nodes.mailbox['s_ji'] # k x m, k = nb_of_nodes_with_m_neighbors
    idx_i = nodes.mailbox['idx_i']
    idx_j = nodes.mailbox['idx_j']
    j_max_score = torch.max(s_ji, dim=1)[1].squeeze()
    nb_nodes = s_ji.size(0)
    idx_range = torch.arange(nb_nodes)
    j_max_index = idx_j[idx_range, j_max_score]
    i_max_index = idx_i[idx_range, j_max_score]
    return {'src_greedy': i_max_index, 'dst_greedy': j_max_index}


def extract_repetitive_pattern(idx_duplicate, val_duplicate):
    k = 0
    for i in idx_duplicate:
        if i in val_duplicate:
            k +=1; val_duplicate.remove(i)
        else:
            break
    return idx_duplicate[:k], val_duplicate
        

def extract_non_duplicate_walk(walk_in):
    walk_in = walk_in.squeeze()
    output, inverse_indices, return_counts = torch.unique(walk_in, sorted=False, return_inverse=True, return_counts=True)
    bool_duplicate = (return_counts!=1)
    if bool_duplicate.sum()<bool_duplicate.size(0): # walk_in has some non-duplicate nodes
        # check if there is a central part with non-duplicate nodes that are consecutives and NOT disconnected
        val_non_duplicate = output[~bool_duplicate]
        indicator_non_duplicate = torch.zeros(walk_in.size(0)).bool()
        for val in val_non_duplicate:
            indicator_non_duplicate[walk_in==val] = True
        # find first and last index of non duplicate nodes
        idx_non_duplicate = torch.arange(walk_in.size(0))[indicator_non_duplicate]
        idx_end_minus_start = idx_non_duplicate[-1] - idx_non_duplicate[0] + 1
        num_non_duplicate = val_non_duplicate.size(0)
        if num_non_duplicate == idx_end_minus_start: # there is a central part with non-duplicate nodes that are consecutives and NOT disconnected
            val_duplicate = output[bool_duplicate]
            len_non_duplicate_walk_in = torch.unique(walk_in).size(0)
            indicator_duplicate = torch.zeros(walk_in.size(0)).bool()
            for val in val_duplicate:
                indicator_duplicate[walk_in==val] = True
            central = walk_in[~indicator_duplicate].tolist()
            idx_central_start = torch.arange(walk_in.size(0))[walk_in==central[0]]
            idx_central_end = torch.arange(walk_in.size(0))[walk_in==central[-1]] + 1
            val_duplicate = val_duplicate.tolist()
            walk_in = walk_in.tolist()
            left_pattern, val_duplicate = extract_repetitive_pattern(walk_in[:idx_central_start][::-1], val_duplicate)
            right_pattern, val_duplicate = extract_repetitive_pattern(walk_in[idx_central_end:], val_duplicate)
            walk_out = left_pattern[::-1] + central + right_pattern
            #assert len(walk_out) == len_non_duplicate_walk_in, f"Length of in and out walks expected to be the same!"
            if len(walk_out) != len_non_duplicate_walk_in: # return empty list for ambiguous duplicate nodes
                walk_out = []    
        else: # return empty list for ambiguous walk (disconnected central part)
            walk_out = []    
    else: # return empty list for ambiguous walk (no central part)
        walk_out = []    
    return walk_out


def compute_walks(g, walks, device): 
    nb_paths = walks.shape[0]
    contigs = []
    clean_walks = []
    clean_walks_length = torch.zeros(nb_paths, device=device).long()
    for k in range(nb_paths):
        walk = walks[k].int()
        clean_walk = extract_non_duplicate_walk(walk) 
        clean_walks.append(clean_walk)
        clean_walks_length[k] = len(clean_walk)
        # print('h2d', k, clean_walks_length[k])
    return clean_walks, clean_walks_length


def parallel_greedy_decoding(original_g, nb_paths, len_threshold, device):

    with torch.no_grad():

        # Remove current self-loop and add new self-loop with -inf score
        original_g = dgl.remove_self_loop(original_g)
        n_original_g = original_g.num_nodes(); self_nodes = torch.arange(n_original_g, dtype=torch.int32).to(device)
        original_g.add_edges(self_nodes, self_nodes)
        original_g.edata['score'][-n_original_g:] = float('-inf')

        # Running lists
        all_walks = []
        all_walks_both_strands = []
        all_walks_len = []
        max_greedy_nb_steps = 0
        idx_contig = 0
        while True:

            idx_contig += 1

            # Track max number of greedy decoding steps
            greedy_nb_steps = 0

            # Extract sub-graph
            if not all_walks_both_strands:
                remove_node_idx = torch.LongTensor([]) # sub-graph = orginal graph 
            else:
                remove_node_idx = torch.LongTensor([item for sublist in all_walks_both_strands for item in sublist])
            list_node_idx = torch.arange(original_g.num_nodes())
            keep_node_idx = torch.ones(original_g.num_nodes())
            keep_node_idx[remove_node_idx] = 0
            keep_node_idx = list_node_idx[keep_node_idx==1].int().to(device)
            #keep_node_idx = torch.randperm(n_original_g)[:80000].int().to(device) # uniform sampling # DEBUG !!!!
            print(f'idx_contig: {idx_contig}, nb_processed_nodes: {n_original_g-keep_node_idx.size(0)}, nb_remaining_nodes: {keep_node_idx.size(0)}, nb_original_nodes: {n_original_g}')
            sub_g = dgl.node_subgraph(original_g, keep_node_idx, store_ids=True) 
            sub_g.ndata['idx_nodes'] = torch.arange(sub_g.num_nodes()).to(device) # index used for max score
            n_sub_g = sub_g.num_nodes()
            print(f'nb of nodes sub-graph: {n_sub_g}')
            
            # Mapping index from sub-graph to original graph
            map_subg_to_g = sub_g.ndata[dgl.NID]

            # Sample initial edges
            prob_edges = torch.sigmoid(sub_g.edata['score']).squeeze()
            prob_edges = prob_edges.masked_fill(prob_edges<1e-9, 1e-9) 
            prob_edges = prob_edges/ prob_edges.sum()
            prob_edges_nb_paths = prob_edges.repeat(nb_paths, 1)
            idx_edges = torch.distributions.categorical.Categorical(prob_edges_nb_paths).sample() # index in sub-graph
            # idx_edges = torch.randperm(sub_g.num_edges())[:nb_paths] # uniform sampling
            src_init_edges = sub_g.edges()[0][idx_edges].long() # index in sub-graph
            dst_init_edges = sub_g.edges()[1][idx_edges].long() # index in sub-graph

            # Forward paths
            g_reverse = dgl.reverse(sub_g, copy_ndata=True, copy_edata=True)
            g_reverse.update_all(greedy_message_func, greedy_reduce_func) 
            src_greedy_forward = g_reverse.ndata['src_greedy'] # index in sub-graph
            dst_greedy_forward = g_reverse.ndata['dst_greedy'] # index in sub-graph
            idx_cur_nodes_subg = dst_init_edges # dst # index in sub-graph
            idx_cur_nodes_g = map_subg_to_g[idx_cur_nodes_subg]
            paths_nodes_forward = [] # index in original graph 
            flag_dead_ends = flag_cycles = True
            nb_steps = 0
            paths_nodes_forward = torch.zeros(nb_paths,n_sub_g//2).long().to(device)
            paths_nodes_forward[:,nb_steps] = idx_cur_nodes_g
            while flag_dead_ends and flag_cycles and nb_steps<n_sub_g//2: # positive or negative strand
                nb_steps += 1
                idx_next_nodes_subg = dst_greedy_forward[idx_cur_nodes_subg] # index in sub-graph
                idx_next_nodes_g = map_subg_to_g[idx_next_nodes_subg] # index in original graph
                paths_nodes_forward[:,nb_steps] = idx_next_nodes_g
                flag_dead_ends = (idx_cur_nodes_subg==idx_next_nodes_subg).long().sum()<nb_paths
                idx_cur_nodes_subg = idx_next_nodes_subg
                if not nb_steps%100: # check cycles
                    for path in paths_nodes_forward:
                        flag_cycles += (torch.unique(path).size(0)<nb_steps)
                    flag_cycles = (flag_cycles<nb_paths) 
            paths_nodes_forward = paths_nodes_forward[:,:nb_steps]
            greedy_nb_steps = nb_steps
            print(f'Forward paths - nb_steps: {nb_steps}, nb_nodes_sub_g: {n_sub_g}, find_cycles: {not flag_cycles}')

            # Backward paths
            sub_g.update_all(greedy_message_func, greedy_reduce_func) 
            src_greedy_backward = sub_g.ndata['src_greedy'] # index in sub-graph
            dst_greedy_backward = sub_g.ndata['dst_greedy'] # index in sub-graph
            idx_cur_nodes_subg = src_init_edges # src # index in sub-graph
            idx_cur_nodes_g = map_subg_to_g[idx_cur_nodes_subg]
            paths_nodes_backward = [] # index in original graph
            flag_dead_ends = flag_cycles = True
            max_cycle_size = 100
            nb_steps = 0
            paths_nodes_backward = torch.zeros(nb_paths,n_sub_g//2).long().to(device)
            paths_nodes_backward[:,nb_steps] = idx_cur_nodes_g
            while flag_dead_ends and flag_cycles and nb_steps<n_sub_g//2: # positive or negative strand
                nb_steps += 1
                idx_next_nodes_subg = dst_greedy_backward[idx_cur_nodes_subg] # index in sub-graph    
                idx_next_nodes_g = map_subg_to_g[idx_next_nodes_subg] # index in original graph
                paths_nodes_backward[:,nb_steps] = idx_next_nodes_g
                flag_dead_ends = (idx_cur_nodes_subg==idx_next_nodes_subg).long().sum()<nb_paths
                idx_cur_nodes_subg = idx_next_nodes_subg
                if not nb_steps%100: # check cycles
                    for path in paths_nodes_backward:
                        flag_cycles += (torch.unique(path).size(0)<nb_steps)
                    flag_cycles = (flag_cycles<nb_paths) 
            paths_nodes_backward = torch.fliplr(paths_nodes_backward[:,:nb_steps])
            greedy_nb_steps += nb_steps; max_greedy_nb_steps = max(max_greedy_nb_steps, greedy_nb_steps)
            print(f'Backward paths - nb_steps: {nb_steps}, nb_nodes_sub_g: {n_sub_g}, find_cycles: {not flag_cycles}')

            # Concatenate forward and backward paths
            paths_nodes = torch.cat( (paths_nodes_backward, paths_nodes_forward), dim=1 )

            # compute total Length of overlaps
            walks, walks_length = compute_walks(original_g, paths_nodes, device) 
            for k in range(nb_paths):
                print(f'candidate path: {k}, walks_length: {walks_length[k]}')

            # Select the path with max total length of overlaps
            #idx_max = torch.argmax(contigs_length)
            idx_max = torch.argmax(walks_length)

            # Append to all walks, contigs
            all_walks.append(walks[idx_max]) 
            all_walks_both_strands.append(walks[idx_max]) # computed strand
            all_walks_both_strands.append([n^1 for n in walks[idx_max]]) # opposite strand
            all_walks_len.append(walks_length[idx_max])
            print(f'idx_contig: {idx_contig}\n')
            print(f'idx of longest contig:  {idx_max}, len of longest walk: {len(walks[idx_max])}\n')

            # criteria to stop decoding
            len_best_walk = torch.max(walks_length, dim=0)[0].squeeze()
            if len_best_walk < len_threshold:
                break

    print(f'max_greedy_nb_steps: {max_greedy_nb_steps}\n')

    all_walks_len = torch.stack(all_walks_len).tolist()

    return all_walks, all_walks_len


def greedy(graph, start, neighbors, preds, edges):

    visited = set()
    current = start
    walk = []

    while current is not None:
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
        neighbor_edges = [edges[(current, n)] for n in neighbors[current]]
        neighbor_lengths = graph.edata['overlap_length'][neighbor_edges]
        _, index = torch.topk(neighbor_lengths, k=1, dim=0)
        current = neighbors[current][index]

    return walk


def baseline(graph, start, neighbors, preds, edges):

    starts = [k for k,v in preds.items() if len(v)==0 and graph.ndata['read_strand'][k]==1]
    walks = []
    # best_read_idx_walks = []
    for start in starts:
        visited = set()
        current = start
        walk = []
        # read_idx_walk = []

        while current is not None:
            if current in visited:
                break
            walk.append(current)
            visited.add(current)
            visited.add(current ^ 1)
            # read_idx_walk.append(graph.ndata['read_idx'][current].item())
            if len(neighbors[current]) == 0:
                break
            if len(neighbors[current]) == 1:
                current = neighbors[current][0]
                continue
            neighbor_edges = [edges[(current, n)] for n in neighbors[current]]
            neighbor_lengths = graph.edata['overlap_length'][neighbor_edges]
            _, index = torch.topk(neighbor_lengths, k=1, dim=0)
            current = neighbors[current][index]

        walks.append(walk.copy())
        # best_read_idx_walks.append(read_idx_walk.copy())

    longest_walk = max(walks, key=lambda x: len(x))
    # sorted(best_read_idx_walks, key=lambda x: len(x))

    return longest_walk


def assert_strand(graph, walk):
    org_strand = graph.ndata['read_strand'][walk[0]].item()
    for idx, node in enumerate(walk[1:]):
        curr_strand = graph.ndata['read_strand'][node].item()
        if curr_strand != org_strand:
            print('-' * 20)
            print(f'walk index: {idx}')
            print(f'node index: {node}')


def assert_overlap(graph, walk):
    for idx, (src, dst) in enumerate(zip(walk[:-1], walk[1:])):
        src_start = graph.ndata['read_start'][src].item()
        dst_start = graph.ndata['read_start'][dst].item()
        src_end = graph.ndata['read_end'][src].item()
        dst_end = graph.ndata['read_end'][dst].item()
        src_strand = graph.ndata['read_strand'][src].item()
        dst_strand = graph.ndata['read_strand'][dst].item()
        if src_strand == dst_strand == 1 and dst_start > src_end:
            print('-' * 20)
            print(f'walk index: {idx}')
            print(f'nodes not connected: {src}, {dst}')
            print(f'end: {src_end}, start: {dst_start}')
        if src_strand == dst_strand == -1 and dst_end < src_start:
            print('-' * 20)
            print(f'walk index: {idx}')
            print(f'nodes not connected: {src}, {dst}')
            print(f'end: {src_start}, start: {dst_end}')



def to_csv(name, root, print_strand=True, save_dir='test_cases'):
    graph = dgl.load_graphs(f'{root}/processed/{name}.dgl')[0][0]
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)
    with open(f'{save_dir}/{name}_info.csv', 'w') as f:
        if print_strand:
            f.write('node_id,read_strand,read_start,read_end\n')
        else:
            f.write('node_id,read_start,read_end\n')
        for n in range(graph.num_nodes()):
            strand = graph.ndata['read_strand'][n].item()
            start = graph.ndata['read_start'][n].item()
            end = graph.ndata['read_end'][n].item()
            if print_strand:
                f.write(f'{n},{strand},{start},{end}\n')
            else:
                if strand == 1:
                    f.write(f'{n},{start},{end}\n')


def to_positive_pairwise(name, root, save_dir='test_cases'):
    graph = dgl.load_graphs(f'{root}/processed/{name}.dgl')[0][0]
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)
    with open(f'{save_dir}/{name}_edges.txt', 'w') as f:
        f.write('src\tdst\n')
        for src, dst in zip(graph.edges()[0], graph.edges()[1]):
            src = src.item()
            dst = dst.item()
            if graph.ndata['read_strand'][src] == 1 and graph.ndata['read_strand'][dst] == 1:
                f.write(f'{src}\t{dst}\n')
    

def get_solutions_for_all_cpp(root, save_dir='test_cases'):
    processed_path = os.path.join(root, 'processed')

    for filename in os.listdir(processed_path):
        name = filename[:-4]
        print(f'Finding walk for... {name}')
        to_csv(name, root, print_strand=False, save_dir=save_dir)
        to_positive_pairwise(name, root, save_dir=save_dir)
        subprocess.run(f'./longestContinuousSequence {name}_info.csv > {name}.out', shell=True, cwd=save_dir)
        with open(f'{save_dir}/{name}.out') as f:
           lines = f.readlines()
           walk = lines[-1].strip().split(' -> ')
           walk = list(map(int, walk))
           pickle.dump(walk, open(f'{save_dir}/{name}_walk.pkl', 'wb'))


def interval_union(name, root):
    graph = dgl.load_graphs(f'{root}/processed/{name}.dgl')[0][0]
    intervals = []
    for strand, start, end in zip(graph.ndata['read_strand'], graph.ndata['read_start'], graph.ndata['read_end']):
        if strand.item() == 1:
            intervals.append([start.item(), end.item()])
    intervals.sort(key=lambda x: x[0])
    result = [intervals[0]]

    for interval in intervals[1:]:
        if interval[0] <= result[-1][1]:
            result[-1][1] = max(result[-1][1], interval[1])
        else:
            result.append(interval)

    return result


def bfs_visit(graph, neighbors, start, all_visited):
    """Get al the nodes in the component, regardless of the strand."""
    queue = deque()
    queue.append(start)
    visited = set()
    while queue:
        # print('here')
        current = queue.popleft()
        if current in visited:
            continue
        if current in all_visited:
            # visited.add(current)
            continue
        visited.add(current)
        queue.extend(neighbors[current])
    return visited


def get_components(graph, neighbors, preds):
    # Connect all the components in the defined manner, regardless of the strands
    components = []
    start = 0
    all_visited = set()
    starts = [n.item() for n in graph.nodes() if len(preds[n.item()]) == 0]
    print(starts)
    for start in starts:
        comp = bfs_visit(graph, neighbors, start, all_visited)
        components.append(comp)
        all_visited = all_visited | set(comp)

    print(len(components))
    changes = True

    while changes:
        changes = False
        components = sorted(components, key=lambda x: len(x))
        comp = components[0]
        for i in range(1, len(components)):
            larger_comp = components[i]
            for node in comp:
                intersect = set(neighbors[node]) & larger_comp
                if len(intersect) > 0:
                    skip = True
                    for joint in intersect:
                        if len(neighbors[joint]) > 0:
                            # Join the two components together
                            skip = False
                            break
                        else:
                            # Don't join them together
                            # Because you can't visit any other nodes from the joint node
                            pass
                    if skip:
                        continue
                    new_comp = comp | larger_comp
                    components.remove(comp)
                    components.remove(larger_comp)
                    components.append(new_comp)
                    changes = True
                    break
            if changes:
                break

#    # Attempt to solve an issue with repeating components
#    threshold = len(components)
#    num_iter = 0
#    while changes or num_iter < threshold:
#        num_iter += 1
#        changes = False
#        components = sorted(components, key=lambda x: len(x))
#        for j, comp in enumerate(components):
#            for i in range(j+1, len(components)):
#                larger_comp = components[i]
#                # if len(comp) == 6:
#                #     print(comp)
#                for node in comp:
#                    # if node == 31098:
#                    #     print('hajhajh')
#                    intersect = set(neighbors[node]) & larger_comp
#                    if len(intersect) > 0:
#                        skip = True
#                        for joint in intersect:
#                            # if joint == 31100:
#                            #     print('here')
#                            if len(neighbors[joint]) > 0:
#                                # Join the two components together
#                                skip = False
#                                break
#                            else:
#                                # Don't join them together
#                                # Because you can't visit any other nodes from the joint node
#                                pass
#                        if skip:
#                            continue
#                        new_comp = comp | larger_comp
#                        components.remove(comp)
#                        components.remove(larger_comp)
#                        components.append(new_comp)
#                        changes = True
#                        break
#                if changes:
#                    break
#            if changes:
#                break

    print(len(components))  # Number of components
    return components


# def dijkstra_gt(graph, neighbors, start, subset):
#     # Here I should take into account strands and reference info
#     # Start = obviously the 0 in-degree node with lowest read-start position
#     # Take the nodes 1 by 1, but only the positive strand and only those that have a reference-overlap
#     dist = {}
#     parent = {}
#     subset = set([node for node in subset if graph.ndata['read_strand'][node] == 1])
#     for node in subset:
#         # dist[node] = math.inf
#         dist[node] = -1
#         parent[node] = None
#     dist[start] = 0
#     while subset:
#         # u = min([dist[v] for v in subset])
#         u = max([dist[v] for v in subset])
#         subset.remove(u)
#         for v in (set(neighbors[u]) & subset):
#             alt = dist[u] + 1
#             if alt > dist[v]:
#                 dist[v] = alt
#                 parent[v] = u
#     return dist, parent


def dfs(graph, neighbors, start=None, avoid={}):
    # TODO: Take only those with in-degree 0
    if start is None:
        min_value, idx = torch.topk(graph.ndata['read_start'], k=1, largest=False)
        start = idx.item()

    # threshold, _ = torch.topk(graph.ndata['read_start'][graph.ndata['read_strand']==1], k=1)
    # threshold = threshold.item()

    stack = deque()
    stack.append(start)

    visited = [True if i in avoid else False for i in range(graph.num_nodes())]

    path = {start: None}
    max_node = start
    max_value = graph.ndata['read_end'][start]

    try:
        while stack:
            current = stack.pop()
            if visited[current]:
                continue
            
            # if graph.ndata['read_end'][current] == threshold:
            #     break

            if graph.ndata['read_end'][current] > max_value:
                max_value = graph.ndata['read_end'][current]
                max_node = current

            visited[current] = True
            tmp = []
            for node in neighbors.get(current, []):
                if visited[node]:
                    continue
                if graph.ndata['read_strand'][node] == -1:
                    continue
                if graph.ndata['read_start'][node] > graph.ndata['read_end'][current]:
                    continue
                if graph.ndata['read_start'][node] < graph.ndata['read_start'][current]:
                    continue
                tmp.append(node)

            if len(tmp) == 0:
                for node in neighbors.get(current, []):
                    if visited[node]:
                        continue
                    if graph.ndata['read_strand'][node] == -1:
                        continue
                    if graph.ndata['read_start'][node] < graph.ndata['read_start'][current]:
                        continue
                    if graph.ndata['read_start'][node] > graph.ndata['read_end'][current]:
                        tmp.append(node)

            tmp.sort(key=lambda x: -graph.ndata['read_start'][x])
            for node in tmp:
                stack.append(node)
                path[node] = current

    except KeyboardInterrupt:
        print("DFS takes too long... Interrupting...")
        pass
    
    finally:
        walk = []
        current = max_node
        while current is not None:
            walk.append(current)
            current = path[current]
        walk.reverse()
        visited = {i for i in range(graph.num_nodes()) if visited[i]}
        return walk, visited


def get_start(graph, strand=1, last=-1):
    bools = torch.logical_and(graph.ndata['read_strand']==strand, graph.ndata['read_start']>last)
    start_pos = graph.ndata['read_start'][bools].min()
    start_idx = (graph.ndata['read_start'] == start_pos).nonzero()[0].item()
    return start_pos, start_idx


def get_correct_edges(graph, neighbors, edges, walk):
    pos_str_edges = set()
    neg_str_edges = set()
    for i, src in enumerate(walk[:-1]):
        for dst in walk[i+1:]:
            if dst in neighbors[src] and graph.ndata['read_start'][dst] < graph.ndata['read_end'][src]:
                try:
                    pos_str_edges.add(edges[(src, dst)])
                except KeyError:
                    print('Edge not found in the edge dictionary')
                    raise
                try:
                    neg_str_edges.add(edges[dst^1, src^1])
                except KeyError:
                    print('Negative strand edge not found in the edge dictionary')
                    raise
            else:
                break
    return pos_str_edges, neg_str_edges


def dfs_gt_graph(graph, neighbors, edges):
    threshold, _ = torch.topk(graph.ndata['read_start'][graph.ndata['read_strand']==1], k=1)
    last_pos = -1
    strand = 1
    all_walks = []
    correct_nodes, correct_edges = set(), set()

    while True:
        print('in')
        start_pos, start_idx = get_start(graph, strand, last_pos)
        neg_str_correct_edges = set()
        walk, _ = dfs(graph, neighbors, start=start_idx)

        print(last_pos, graph.ndata['read_end'][walk[-1]])
        print(len(walk))
        print(start_idx)
        all_walks.append(walk)

        correct_nodes = correct_nodes | set(walk)

        pos_str_edges, neg_str_edges = get_correct_edges(graph, neighbors, edges, walk)
        correct_edges = correct_edges | pos_str_edges
        neg_str_correct_edges = neg_str_correct_edges | neg_str_edges
        
        last_pos = graph.ndata['read_end'][walk[-1]]
        print(last_pos)
        if last_pos > 0.99 * threshold:
            break

    neg_str_correct_nodes = set([n^1 for n in correct_nodes])

    return correct_nodes, correct_edges, neg_str_correct_nodes, neg_str_correct_edges, all_walks


def dfs_gt_neurips_graph(graph, neighbors, edges):
    all_nodes = {i for i in range(graph.num_nodes()) if graph.ndata['read_strand'][i] == 1}
    last_node = max(all_nodes, key=lambda x: graph.ndata['read_end'][x])

    largest_visited = -1
    all_walks = []
    pos_correct_edges, neg_correct_edges = set(), set()
    all_visited = set()

    while all_nodes:
        start = min(all_nodes, key=lambda x: graph.ndata['read_start'][x])
        walk, visited = dfs(graph, neighbors, start, avoid=all_visited)
        if graph.ndata['read_end'][walk[-1]] < largest_visited or len(walk) == 1:
            all_nodes = all_nodes - visited
            all_visited = all_visited | visited
            # print(f'\nDiscard component')
            # print(f'Start = {graph.ndata["read_start"][walk[0]]}\t Node = {walk[0]}')
            # print(f'End   = {graph.ndata["read_end"][walk[-1]]}\t Node = {walk[-1]}')
            # print(f'Walk length = {len(walk)}')
            continue
        else:
            largest_visited = graph.ndata['read_end'][walk[-1]]
            all_walks.append(walk)

        # print(f'\nInclude component')
        # print(f'Start = {graph.ndata["read_start"][walk[0]]}\t Node = {walk[0]}')
        # print(f'End   = {graph.ndata["read_end"][walk[-1]]}\t Node = {walk[-1]}')
        # print(f'Walk length = {len(walk)}')

        pos_str_edges, neg_str_edges = get_correct_edges(graph, neighbors, edges, walk)
        pos_correct_edges = pos_correct_edges | pos_str_edges
        neg_correct_edges = neg_correct_edges | neg_str_edges

        if largest_visited == graph.ndata['read_end'][last_node]:
            break
        all_nodes = all_nodes - visited
        all_visited = all_visited | visited

    return pos_correct_edges, neg_correct_edges



def dfs_gt_another(graph, neighbors, preds):
    components = get_components(graph, neighbors, preds)
    # components = [c for c in components if len(c) >= 10]
    components = sorted(components, key=lambda x: -len(x))
    walks = []
    for i, component in enumerate(components):
        try:
            start_nodes = [node for node in component if len(preds[node]) == 0 and graph.ndata['read_strand'][node] == 1]
            start = min(start_nodes, key=lambda x: graph.ndata['read_start'][x])
            walk = dfs(graph, neighbors, start)
            print(f'Component {i}: length = {len(walk)}')
            print(f'\tStart: {graph.ndata["read_start"][walk[0]]}\tEnd: {graph.ndata["read_end"][walk[-1]]}')
            walks.append(walk)
        except ValueError:
            # len(start_nodes) == 0
            # Means it's a negative-strand component, neglect for now
            # TODO: Solve later
            print(f'Component {i}: passed')
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


def get_solutions_for_all(data_path, threshold=None):
    processed_path = f'{data_path}/processed'
    neighbors_path = f'{data_path}/info'
    solutions_path = f'{data_path}/solutions'
    if not os.path.isdir(solutions_path):
        os.mkdir(solutions_path)
    for name in os.listdir(processed_path):
        idx = name[:-4]
        print(idx)
        graph = dgl.load_graphs(os.path.join(processed_path, name))[0][0]
        neighbors = pickle.load(open(os.path.join(neighbors_path, idx + '_succ.pkl'), 'rb'))
        preds = pickle.load(open(os.path.join(neighbors_path, idx + '_pred.pkl'), 'rb'))
        walks = dfs_gt_another(graph, neighbors, preds)
        # walk = dfs_gt_forwards(graph, neighbors, threshold=threshold)
        pickle.dump(walks, open(os.path.join(solutions_path, idx + '_gt.pkl'), 'wb'))
