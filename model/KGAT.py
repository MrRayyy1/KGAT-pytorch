import dgl
import torch
import torch.nn as nn
import torch.nn.functional as F
from dgl.nn.pytorch.softmax import edge_softmax
from utility.helper import edge_softmax_fix
import numpy as np


def _L2_loss_mean(x):
    return torch.mean(torch.sum(torch.pow(x, 2), dim=1, keepdim=False) / 2.)


class Aggregator(nn.Module):

    def __init__(self, in_dim, out_dim, dropout, aggregator_type):
        super(Aggregator, self).__init__()
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.dropout = dropout
        self.aggregator_type = aggregator_type

        self.message_dropout = nn.Dropout(dropout)

        if aggregator_type == 'gcn':
            self.W = nn.Linear(self.in_dim, self.out_dim)       # W in Equation (6)
        elif aggregator_type == 'graphsage':
            self.W = nn.Linear(self.in_dim * 2, self.out_dim)   # W in Equation (7)
        elif aggregator_type == 'bi-interaction':
            self.W1 = nn.Linear(self.in_dim, self.out_dim)      # W1 in Equation (8)
            self.W2 = nn.Linear(self.in_dim, self.out_dim)      # W2 in Equation (8)
        else:
            raise NotImplementedError

        self.activation = nn.LeakyReLU()


    def forward(self, mode, g, entity_embed):
        g = g.local_var()
        g.ndata['node'] = entity_embed

        # Equation (3) & (10)
        # DGL: dgl-cu90(0.4.1)
        # Get different results when using `dgl.function.sum`, and the randomness is due to `atomicAdd`
        # Use `dgl.function.sum` when training model to speed up
        # Use custom function to ensure deterministic behavior when predicting
        if mode == 'predict':
            g.update_all(dgl.function.u_mul_e('node', 'att', 'side'), lambda nodes: {'N_h': torch.sum(nodes.mailbox['side'], 1)})
        else:
            g.update_all(dgl.function.u_mul_e('node', 'att', 'side'), dgl.function.sum('side', 'N_h'))

        if self.aggregator_type == 'gcn':
            # Equation (6) & (9)
            out = self.activation(self.W(g.ndata['node'] + g.ndata['N_h']))                         # (n_users + n_entities, out_dim)

        elif self.aggregator_type == 'graphsage':
            # Equation (7) & (9)
            out = self.activation(self.W(torch.cat([g.ndata['node'], g.ndata['N_h']], dim=1)))      # (n_users + n_entities, out_dim)

        elif self.aggregator_type == 'bi-interaction':
            # Equation (8) & (9)
            out1 = self.activation(self.W1(g.ndata['node'] + g.ndata['N_h']))                       # (n_users + n_entities, out_dim)
            out2 = self.activation(self.W2(g.ndata['node'] * g.ndata['N_h']))                       # (n_users + n_entities, out_dim)
            out = out1 + out2
        else:
            raise NotImplementedError

        out = self.message_dropout(out)
        return out


class KGAT(nn.Module):

    def __init__(self, args,
                 n_users, n_entities, n_relations,
                 user_pre_embed=None, item_pre_embed=None):

        super(KGAT, self).__init__()
        self.use_pretrain = args.use_pretrain

        self.n_users = n_users
        self.n_entities = n_entities
        self.n_relations = n_relations

        self.entity_dim = args.entity_dim
        self.relation_dim = args.relation_dim

        self.aggregation_type = args.aggregation_type
        self.conv_dim_list = [args.entity_dim] + eval(args.conv_dim_list)
        self.mess_dropout = eval(args.mess_dropout)
        self.n_layers = len(eval(args.conv_dim_list))

        self.kg_l2loss_lambda = args.kg_l2loss_lambda
        self.cf_l2loss_lambda = args.cf_l2loss_lambda

        self.relation_embed = nn.Embedding(self.n_relations, self.relation_dim)
        #self.entity_user_embed = nn.Embedding(self.n_entities + self.n_users, self.entity_dim)
        self.entity_embed = nn.Embedding(self.n_entities, self.entity_dim)
        if (self.use_pretrain == 1) and (user_pre_embed is not None) and (item_pre_embed is not None):
            other_entity_embed = nn.Parameter(torch.Tensor(self.n_entities - item_pre_embed.shape[0], self.entity_dim))
            nn.init.xavier_uniform_(other_entity_embed, gain=nn.init.calculate_gain('relu'))
            entity_embed = torch.cat([item_pre_embed, other_entity_embed], dim=0)
            self.entity_embed.weight = nn.Parameter(entity_embed)

        self.W_R = nn.Parameter(torch.Tensor(self.n_relations, self.entity_dim, self.relation_dim))
        nn.init.xavier_uniform_(self.W_R, gain=nn.init.calculate_gain('relu'))

        self.aggregator_layers = nn.ModuleList()
        for k in range(self.n_layers):
            self.aggregator_layers.append(Aggregator(self.conv_dim_list[k], self.conv_dim_list[k + 1], self.mess_dropout[k], self.aggregation_type))
        self.user_encoder = nn.Sequential(
            nn.Linear(176*2, 176, bias=False),
            nn.ReLU(),
            nn.Linear(176, 176, bias = False),
            nn.ReLU()
        )
        for layer in self.user_encoder:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)

        self.item_encoder = nn.Sequential(
            nn.Linear(176 * 2, 176, bias=False),
            nn.ReLU(),
            nn.Linear(176, 176, bias=False),
            nn.ReLU()
        )
        for layer in self.item_encoder:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)

    def att_score(self, edges):
        # Equation (4)
        r_mul_t = torch.matmul(self.entity_embed(edges.src['id']), self.W_r)                       # (n_edge, relation_dim)
        r_mul_h = torch.matmul(self.entity_embed(edges.dst['id']), self.W_r)                       # (n_edge, relation_dim)
        r_embed = self.relation_embed(edges.data['type'])                                               # (1, relation_dim)
        att = torch.bmm(r_mul_t.unsqueeze(1), torch.tanh(r_mul_h + r_embed).unsqueeze(2)).squeeze(-1)   # (n_edge, 1)
        return {'att': att}


    def compute_attention(self, g):
        g = g.local_var()
        for i in range(self.n_relations):
            edge_idxs = g.filter_edges(lambda edge: edge.data['type'] == i)
            self.W_r = self.W_R[i]
            g.apply_edges(self.att_score, edge_idxs)

        # Equation (5)
        g.edata['att'] = edge_softmax_fix(g, g.edata.pop('att'))
        return g.edata.pop('att')


    def calc_kg_loss(self, h, r, pos_t, neg_t):
        """
        h:      (kg_batch_size)
        r:      (kg_batch_size)
        pos_t:  (kg_batch_size)
        neg_t:  (kg_batch_size)
        """
        r_embed = self.relation_embed(r)                 # (kg_batch_size, relation_dim)
        W_r = self.W_R[r]                                # (kg_batch_size, entity_dim, relation_dim)

        h_embed = self.entity_embed(h)              # (kg_batch_size, entity_dim)
        pos_t_embed = self.entity_embed(pos_t)      # (kg_batch_size, entity_dim)
        neg_t_embed = self.entity_embed(neg_t)      # (kg_batch_size, entity_dim)

        r_mul_h = torch.bmm(h_embed.unsqueeze(1), W_r).squeeze(1)             # (kg_batch_size, relation_dim)
        r_mul_pos_t = torch.bmm(pos_t_embed.unsqueeze(1), W_r).squeeze(1)     # (kg_batch_size, relation_dim)
        r_mul_neg_t = torch.bmm(neg_t_embed.unsqueeze(1), W_r).squeeze(1)     # (kg_batch_size, relation_dim)

        # Equation (1)
        pos_score = torch.sum(torch.pow(r_mul_h + r_embed - r_mul_pos_t, 2), dim=1)     # (kg_batch_size)
        neg_score = torch.sum(torch.pow(r_mul_h + r_embed - r_mul_neg_t, 2), dim=1)     # (kg_batch_size)

        # Equation (2)
        kg_loss = (-1.0) * F.logsigmoid(neg_score - pos_score)
        kg_loss = torch.mean(kg_loss)

        l2_loss = _L2_loss_mean(r_mul_h) + _L2_loss_mean(r_embed) + _L2_loss_mean(r_mul_pos_t) + _L2_loss_mean(r_mul_neg_t)
        loss = kg_loss + self.kg_l2loss_lambda * l2_loss
        return loss


    def cf_embedding(self, mode, g):
        g = g.local_var()
        ego_embed = self.entity_embed(g.ndata['id'])
        all_embed = [ego_embed]

        for i, layer in enumerate(self.aggregator_layers):
            ego_embed = layer(mode, g, ego_embed)
            norm_embed = F.normalize(ego_embed, p=2, dim=1)
            all_embed.append(norm_embed)

        # Equation (11)
        all_embed = torch.cat(all_embed, dim=1)         # (n_users + n_entities, cf_concat_dim)
        return all_embed


    def cf_score(self, mode, g, user_ids, item_ids,user_dict, sim_user_dict, sim_item_dict, item_dict):
        """
        user_ids:   number of users to evaluate   (n_eval_users)
        item_ids:   number of items to evaluate   (n_eval_items)
        """
        all_embed = self.cf_embedding(mode, g)          # (n_users + n_entities, cf_concat_dim)
        #user_embed = all_embed[user_ids]                # (n_eval_users, cf_concat_dim)
        user_embed = self.get_user_embed(all_embed, user_ids, user_dict, sim_user_dict)
        #item_embed = all_embed[item_ids]                # (n_eval_items, cf_concat_dim)
        item_embed = self.get_item_embed(all_embed, item_ids, sim_item_dict, item_dict)
        # Equation (12)
        cf_score = torch.matmul(user_embed, item_embed.transpose(0, 1))    # (n_eval_users, n_eval_items)
        return cf_score

    def get_user_kg_embed(self, embed, user_ids, user_dict):
        kg_user_embed = None
        if isinstance(user_ids, np.float64):
            interacted_item_list = user_dict[user_ids.item()]
            kg_user_embed = (torch.sum(embed[interacted_item_list], dim = 0)/len(interacted_item_list)).view(1,-1)
        else:
            for uid in user_ids:
                interacted_item_list = user_dict[uid.item()]
                _embed = torch.sum(embed[interacted_item_list], dim = 0)/len(interacted_item_list)
                if kg_user_embed is None:
                    kg_user_embed = _embed
                else:
                    kg_user_embed = torch.cat((kg_user_embed, _embed), dim=0)
            kg_user_embed = kg_user_embed.view(len(user_ids), -1)
        return kg_user_embed

    def get_user_cf_embed(self, embed, user_ids, user_dict, sim_user_dict):
        cf_user_embed = None
        for uid in user_ids:
            sim_user_embed = 0
            norm = 0
            for sim_user in sim_user_dict[uid.item()]:
                sim = self.calk_sim(uid.item(), sim_user, user_dict)
                sim_user_embed += sim * self.get_user_kg_embed(embed, sim_user, user_dict)
                norm += sim
            sim_user_embed_final = sim_user_embed / norm
            if cf_user_embed is None:
                cf_user_embed = sim_user_embed_final
            else:
                cf_user_embed = torch.cat((cf_user_embed, sim_user_embed_final), dim=0)
        return cf_user_embed.view(len(user_ids), -1)

    def get_user_embed(self, embed, user_ids, user_dict, sim_user_dict):
        kg_embed = self.get_user_kg_embed(embed, user_ids, user_dict)
        cf_embed = self.get_user_cf_embed(embed, user_ids, user_dict, sim_user_dict)
        final_embed = torch.cat((kg_embed, cf_embed), dim=-1)
        user_embed = self.user_encoder(final_embed)
        return user_embed

    def get_item_embed(self,all_embed, item_ids, sim_item_dict, item_dict):
        item_kg_embed = all_embed[item_ids]
        cf_item_embed = None
        for iid in item_ids:
            sim_item_embed = 0
            norm = 0
            for sim_item in sim_item_dict[iid.item()]:
                sim = self.calk_sim(iid.item(), sim_item, item_dict)
                sim_item_embed +=  sim * all_embed[sim_item.astype(int)]
                norm += sim
            sim_item_embed_final = sim_item_embed / norm
            if cf_item_embed is None:
                cf_item_embed = sim_item_embed_final
            else:
                cf_item_embed = torch.cat((cf_item_embed, sim_item_embed_final), dim=0)
        cf_item_embed = cf_item_embed.view(len(item_ids), -1)
        item_embed_final = torch.cat((item_kg_embed, cf_item_embed), dim=-1)
        item_embed = self.item_encoder(item_embed_final)
        return item_embed

    def calk_sim(self,idx, sim_idx, i_dict):
        staffs = i_dict[idx]
        staffs2 = i_dict[sim_idx]
        all = len(list(set(staffs).union(set(staffs2))))
        inter = len(list(set(staffs).intersection(set(staffs2))))
        sim = inter/all
        return sim


    def calc_cf_loss(self, mode, g, user_ids, item_pos_ids, item_neg_ids, user_dict, sim_user_dict, item_dict, sim_item_dict):
        """
        user_ids:       (cf_batch_size)
        item_pos_ids:   (cf_batch_size)
        item_neg_ids:   (cf_batch_size)
        """
        all_embed = self.cf_embedding(mode, g)                      # (n_users + n_entities, cf_concat_dim)
        #user_embed = all_embed[user_ids]                            # (cf_batch_size, cf_concat_dim)
        user_embed = self.get_user_embed(all_embed, user_ids, user_dict, sim_user_dict)
        #item_pos_embed = all_embed[item_pos_ids]                    # (cf_batch_size, cf_concat_dim)
        #item_neg_embed = all_embed[item_neg_ids]                    # (cf_batch_size, cf_concat_dim)
        item_pos_embed = self.get_item_embed(all_embed, item_pos_ids, sim_item_dict, item_dict)
        item_neg_embed = self.get_item_embed(all_embed, item_neg_ids, sim_item_dict, item_dict)
        # Equation (12)
        pos_score = torch.sum(user_embed * item_pos_embed, dim=1)   # (cf_batch_size)
        neg_score = torch.sum(user_embed * item_neg_embed, dim=1)   # (cf_batch_size)

        # Equation (13)
        cf_loss = (-1.0) * F.logsigmoid(pos_score - neg_score)
        cf_loss = torch.mean(cf_loss)

        l2_loss = _L2_loss_mean(user_embed) + _L2_loss_mean(item_pos_embed) + _L2_loss_mean(item_neg_embed)
        loss = cf_loss + self.cf_l2loss_lambda * l2_loss
        return loss


    def forward(self, mode, *input):
        if mode == 'calc_att':
            return self.compute_attention(*input)
        if mode == 'calc_cf_loss':
            return self.calc_cf_loss(mode, *input)
        if mode == 'calc_kg_loss':
            return self.calc_kg_loss(*input)
        if mode == 'predict':
            return self.cf_score(mode, *input)


