'''
Created on Oct, 2019

@author: hugo

'''
import json
import os
import random
import numpy as np
from collections import Counter

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau
import torch.nn.functional as F

from .models.seq2seq import Seq2Seq
from .models.graph2seq import Graph2Seq
from .utils.vocab_utils import VocabModel
from .utils import constants as Constants
from .utils.generic_utils import to_cuda, create_mask
from .utils.io_utils import load_ndjson
from .evaluation.eval import QGEvalCap, WMD
from .utils.constants import INF
from .layers.common import dropout



class Model(object):
    """High level model that handles intializing the underlying network
    architecture, saving, updating examples, and predicting examples.
    """
    def __init__(self, config, train_set=None):
        self.config = config
        if config['model_name'] == 'graph2seq':
            self.net_module = Graph2Seq
        elif config['model_name'] == 'seq2seq':
            self.net_module = Seq2Seq
        else:
            raise RuntimeError('Unknown model_name: {}'.format(config['model_name']))
        print('[ Running {} model ]'.format(config['model_name']))

        self.vocab_model = VocabModel.build(self.config['saved_vocab_file'], train_set, config)
        if config['kg_emb']:
            self.config['num_entities'] = len(self.vocab_model.node_vocab)
            self.config['num_entity_types'] = len(self.vocab_model.node_type_vocab)
            self.config['num_relations'] = len(self.vocab_model.edge_type_vocab)
        else:
            self.vocab_model.node_vocab = None
            self.vocab_model.node_type_vocab = None
            self.vocab_model.edge_type_vocab = None


        if self.config['pretrained']:
            state_dict_opt = self.init_saved_network(self.config['pretrained'])
        else:
            assert train_set is not None
            # Building network.
            self._init_new_network()

        num_params = 0
        for name, p in self.network.named_parameters():
            print('{}: {}'.format(name, str(p.size())))
            num_params += p.numel()

        if self.config['use_bert'] and self.config.get('finetune_bert', None):
            for name, p in self.config['bert_model'].named_parameters():
                print('{}: {}'.format(name, str(p.size())))
                num_params += p.numel()
        print('#Parameters = {}\n'.format(num_params))

        self.criterion = nn.NLLLoss(ignore_index=self.vocab_model.word_vocab.PAD)
        self._init_optimizer()


        if config['rl_wmd_ratio'] > 0:
            self.wmd = WMD(config.get('wmd_emb_file', None))
        else:
            self.wmd = None


    def init_saved_network(self, saved_dir):
        _ARGUMENTS = ['word_embed_dim', 'hidden_size',
                      'word_dropout', 'rnn_dropout',
                      'ctx_graph_hops', 'ctx_graph_topk',
                      'score_unk_threshold', 'score_yes_threshold',
                      'score_no_threshold']

        # Load all saved fields.
        fname = os.path.join(saved_dir, Constants._SAVED_WEIGHTS_FILE)
        print('[ Loading saved model %s ]' % fname)
        saved_params = torch.load(fname, map_location=lambda storage, loc: storage)
        state_dict = saved_params['state_dict']
        self.saved_epoch = saved_params.get('epoch', 0)

        # for k in _ARGUMENTS:
        #     if saved_params['config'][k] != self.config[k]:
        #         print('Overwrite {}: {} -> {}'.format(k, self.config[k], saved_params['config'][k]))
        #         self.config[k] = saved_params['config'][k]

        w_embedding = self._init_embedding(len(self.vocab_model.word_vocab), self.config['word_embed_dim'])
        self.network = self.net_module(self.config, w_embedding, self.vocab_model.word_vocab)

        # Merge the arguments
        if state_dict:
            merged_state_dict = self.network.state_dict()
            for k, v in state_dict['network'].items():
                if k in merged_state_dict:
                    merged_state_dict[k] = v
            self.network.load_state_dict(merged_state_dict)

        if self.config['use_bert'] and self.config.get('finetune_bert', None):
            self.config['bert_model'].load_state_dict(state_dict['bert'])

        return state_dict.get('optimizer', None) if state_dict else None

    def _init_new_network(self):
        w_embedding = self._init_embedding(len(self.vocab_model.word_vocab), self.config['word_embed_dim'],
                                           pretrained_vecs=self.vocab_model.word_vocab.embeddings)

        if self.config['kg_emb']:
            pretrained_entity_vecs, pretrained_relation_vecs = self.load_pretrained_kg_emb(self.vocab_model.node_vocab, self.vocab_model.edge_type_vocab)


            entity_emb = self._init_embedding(len(self.vocab_model.node_vocab), self.config['entity_emb_dim'],
                                            pretrained_vecs=pretrained_entity_vecs)
            relation_emb = self._init_embedding(len(self.vocab_model.edge_type_vocab), self.config['relation_emb_dim'],
                                            pretrained_vecs=pretrained_relation_vecs)
            del pretrained_entity_vecs
            del pretrained_relation_vecs

        else:
            entity_emb = None
            relation_emb = None

        self.network = self.net_module(self.config, w_embedding, self.vocab_model.word_vocab, entity_emb=entity_emb, relation_emb=relation_emb)

    def load_pretrained_kg_emb(self, node_vocab, edge_type_vocab):
        ent2vec = load_ndjson(self.config['pretrained_entity_embed_file'], return_type='dict')

        pretrained_entity_vecs = []
        for entity in node_vocab.index2word:
            if entity in ent2vec:
                pretrained_entity_vecs.append(ent2vec[entity])
            else:
                pretrained_entity_vecs.append(np.array(np.random.uniform(low=-0.1, high=0.1, size=(self.config['entity_emb_dim'],)), dtype=np.float32))
        pretrained_entity_vecs = np.array(pretrained_entity_vecs)


        rel2vec = load_ndjson(self.config['pretrained_relation_embed_file'], return_type='dict')

        pretrained_relation_vecs = []
        for relation in edge_type_vocab.index2word:
            if relation in rel2vec:
                pretrained_relation_vecs.append(rel2vec[relation])
            else:
                pretrained_relation_vecs.append(np.array(np.random.uniform(low=-0.1, high=0.1, size=(self.config['relation_emb_dim'],)), dtype=np.float32))
        pretrained_relation_vecs = np.array(pretrained_relation_vecs)
        return pretrained_entity_vecs, pretrained_relation_vecs

    def _init_optimizer(self):
        parameters = [p for p in self.network.parameters() if p.requires_grad]
        if self.config['use_bert'] and self.config.get('finetune_bert', None):
            parameters += [p for p in self.config['bert_model'].parameters() if p.requires_grad]
        if self.config['optimizer'] == 'sgd':
            self.optimizer = optim.SGD(parameters, self.config['learning_rate'],
                                       momentum=self.config['momentum'],
                                       weight_decay=self.config['weight_decay'])
        elif self.config['optimizer'] == 'adam':
            self.optimizer = optim.Adam(parameters, lr=self.config['learning_rate'])
        elif self.config['optimizer'] == 'adamax':
            self.optimizer = optim.Adamax(parameters, lr=self.config['learning_rate'])
        else:
            raise RuntimeError('Unsupported optimizer: %s' % self.config['optimizer'])
        self.scheduler = ReduceLROnPlateau(self.optimizer, mode='max', factor=0.5, \
                    patience=2, verbose=True)

    def _init_embedding(self, vocab_size, embed_size, pretrained_vecs=None):
        """Initializes the embeddings
        """
        return nn.Embedding(vocab_size, embed_size, padding_idx=0,
                            _weight=torch.from_numpy(pretrained_vecs).float()
                            if pretrained_vecs is not None else None)

    def save(self, dirname, epoch):
        params = {
            'state_dict': {
                'network': self.network.state_dict(),
                'bert': self.config['bert_model'].state_dict() if self.config['use_bert'] and self.config.get('finetune_bert', None) else None,
                'optimizer': self.optimizer.state_dict()
            },
            'config': self.config,
            'dir': dirname,
            'epoch': epoch
        }
        try:
            torch.save(params, os.path.join(dirname, Constants._SAVED_WEIGHTS_FILE))
        except BaseException:
            print('[ WARN: Saving failed... continuing anyway. ]')

    def predict(self, batch, step, forcing_ratio=1, rl_ratio=0, update=True, out_predictions=False, mode='train'):
        self.network.train(update)

        if mode == 'train':
            loss, loss_value, metrics = train_batch(batch, self.network, self.vocab_model.word_vocab, self.criterion, forcing_ratio, rl_ratio, self.config, wmd=self.wmd)

            # Accumulate gradients
            loss = loss / self.config['grad_accumulated_steps'] # Normalize our loss (if averaged)
            # Run backward
            loss.backward()

            if (step + 1) % self.config['grad_accumulated_steps'] == 0: # Wait for several backward steps
                if self.config['grad_clipping']:
                    # Clip gradients
                    parameters = [p for p in self.network.parameters() if p.requires_grad]
                    if self.config['use_bert'] and self.config.get('finetune_bert', None):
                        parameters += [p for p in self.config['bert_model'].parameters() if p.requires_grad]

                    torch.nn.utils.clip_grad_norm_(parameters, self.config['grad_clipping'])
                # Update parameters
                self.optimizer.step()
                self.optimizer.zero_grad()

        elif mode == 'dev':
            decoded_batch, loss_value, metrics = dev_batch(batch, self.network, self.vocab_model.word_vocab, criterion=None, show_cover_loss=self.config['show_cover_loss'])

        elif mode == 'generate':
            decoded = generate_batch(batch, self.network, self.vocab_model.word_vocab, self.config)
            loss_value, metrics = None, None

        else:
            decoded_batch, metrics = test_batch(batch, self.network, self.vocab_model.word_vocab, self.config)
            loss_value = None

        output = {
            'loss': loss_value,
            'metrics': metrics
        }

        if mode == 'test' and out_predictions:
            output['predictions'] = decoded_batch
        if mode == 'generate' and out_predictions:
            output['predictions'] = decoded
        return output


# Training phase
def train_batch(batch, network, vocab, criterion, forcing_ratio, rl_ratio, config, wmd=None):
    network.train(True)

    with torch.set_grad_enabled(True):
        ext_vocab_size = batch['oov_dict'].ext_vocab_size if batch['oov_dict'] else None

        network_out = network(batch, batch['targets'], criterion,
                forcing_ratio=forcing_ratio, partial_forcing=config['partial_forcing'], \
                sample=config['sample'], ext_vocab_size=ext_vocab_size, \
                include_cover_loss=config['show_cover_loss'])

        if rl_ratio > 0:
            batch_size = batch['batch_size']
            sample_out = network(batch, saved_out=network_out, criterion=criterion, \
                    criterion_reduction=False, criterion_nll_only=True, \
                    sample=True, ext_vocab_size=ext_vocab_size)
            baseline_out = network(batch, saved_out=network_out, visualize=False, \
                                    ext_vocab_size=ext_vocab_size)

            sample_out_decoded = sample_out.decoded_tokens.transpose(0, 1)
            baseline_out_decoded = baseline_out.decoded_tokens.transpose(0, 1)

            neg_reward = []
            rl_reward_metric_list = config['rl_reward_metric'].split(',')
            if 'rl_reward_metric_ratio' in config:
                rl_reward_metric_ratio = [float(x) for x in config['rl_reward_metric_ratio'].split(',')]
            else:
                rl_reward_metric_ratio = None

            for i in range(batch_size):
                scores = eval_batch_output([batch['target_src'][i]], vocab, batch['oov_dict'],
                                       [sample_out_decoded[i]], [baseline_out_decoded[i]])

                # greedy_score = scores[1][config['rl_reward_metric']]
                # reward_ = scores[0][config['rl_reward_metric']] - greedy_score
                reward_ = 0
                for index, rl_reward_metric in enumerate(rl_reward_metric_list):
                    greedy_score = scores[1][rl_reward_metric]
                    tmp_reward_ = (scores[0][rl_reward_metric] - greedy_score)
                    if rl_reward_metric_ratio is not None:
                        tmp_reward_ = tmp_reward_ * rl_reward_metric_ratio[index]

                    reward_ += tmp_reward_


                if config['rl_wmd_ratio'] > 0:
                    # Add word mover's distance
                    sample_seq = batch_decoded_index2word([sample_out_decoded[i]], vocab, batch['oov_dict'])[0]
                    greedy_seq = batch_decoded_index2word([baseline_out_decoded[i]], vocab, batch['oov_dict'])[0]

                    sample_wmd = -wmd.distance(sample_seq, batch['target_src'][i]) / max(len(sample_seq.split()), 1)
                    greedy_wmd = -wmd.distance(greedy_seq, batch['target_src'][i]) / max(len(greedy_seq.split()), 1)
                    wmd_reward_ = sample_wmd - greedy_wmd
                    wmd_reward_ = max(min(wmd_reward_, config['max_wmd_reward']), -config['max_wmd_reward'])
                    reward_ += config['rl_wmd_ratio'] * wmd_reward_

                neg_reward.append(reward_)
            neg_reward = to_cuda(torch.Tensor(neg_reward), network.device)


            # if sample > baseline, the reward is positive (i.e. good exploration), rl_loss is negative
            rl_loss = torch.sum(neg_reward * sample_out.loss) / batch_size
            rl_loss_value = torch.sum(neg_reward * sample_out.loss_value).item() / batch_size
            loss = (1 - rl_ratio) * network_out.loss + rl_ratio * rl_loss
            loss_value = (1 - rl_ratio) * network_out.loss_value + rl_ratio * rl_loss_value

            metrics = eval_batch_output(batch['target_src'], vocab, \
                            batch['oov_dict'], baseline_out.decoded_tokens)[0]

        else:
            loss = network_out.loss
            loss_value = network_out.loss_value
            metrics = eval_batch_output(batch['target_src'], vocab, \
                            batch['oov_dict'], network_out.decoded_tokens)[0]

    return loss, loss_value, metrics

# Development phase
def dev_batch(batch, network, vocab, criterion=None, show_cover_loss=False):
  """Test the `network` on the `batch`, return the ROUGE score and the loss."""
  network.train(False)
  decoded_batch, out = eval_decode_batch(batch, network, vocab, criterion=criterion, show_cover_loss=show_cover_loss)
  metrics = evaluate_predictions(batch['target_src'], decoded_batch)
  return decoded_batch, out.loss_value, metrics


# Testing phase
def test_batch(batch, network, vocab, config):
    network.train(False)
    decoded_batch = beam_search(batch, network, vocab, config)
    metrics = evaluate_predictions(batch['target_src'], decoded_batch)
    return decoded_batch, metrics


def generate_batch(batch, network, vocab, config):
    network.train(False)
    with torch.no_grad():
        ext_vocab_size = batch['oov_dict'].ext_vocab_size
        oov_dict = batch['oov_dict']
        ent_idx = batch['in_graphs']['g_ent_idx']
        hypotheses = batch_beam_search(network, batch, ext_vocab_size,
                                       config['beam_size'], min_out_len=config['min_out_len'],
                                       max_out_len=config['max_out_len'],
                                       len_in_words=config['out_len_in_words'],
                                       block_ngram_repeat=config['block_ngram_repeat'])

        to_decode = [each[0].tokens[1:] for each in hypotheses]  # the first token is SOS
        decoded = []
        for i, doc in enumerate(to_decode):
            decoded_doc, topic_ents = [], []
            for word_idx in doc:
                if word_idx == vocab.SOS:
                    continue
                if word_idx == vocab.EOS:
                    break

                if word_idx >= len(vocab):
                    topic_ents.append(ent_idx[i][word_idx])
                    word = oov_dict.index2word.get((i, word_idx), vocab.unk_token)
                    word = ' '.join(word)
                else:
                    word = vocab.getWord(word_idx)
                decoded_doc.append(word)
            decoded.append(json.dumps({
                'questions': ' '.join(decoded_doc),
                'topic entities': topic_ents,
                'src_id': batch["src_id"][i] if batch["src_id"] else None,
                'q_id': batch['q_id'][i]
            }))
        return decoded



def eval_batch_output(target_src, vocab, oov_dict, *pred_tensors):
  """
  :param target_src: the gold standard, as textual tokens
  :param vocab: the fixed-size vocab
  :param oov_dict: out-of-vocab dict
  :param pred_tensors: one or more systems' prediction (output tensors)
  :return: two-level score lookup (system index => ROUGE metric => value)

  Evaluate one or more systems' output.
  """
  decoded_batch = [batch_decoded_index2word(pred_tensor, vocab, oov_dict)
                   for pred_tensor in pred_tensors]
  metrics = [evaluate_predictions(target_src, x) for x in decoded_batch]
  return metrics

def eval_decode_batch(batch, network, vocab, criterion=None, show_cover_loss=False):
  """Test the `network` on the `batch`, return the decoded textual tokens and the Output."""
  with torch.no_grad():
    ext_vocab_size = batch['oov_dict'].ext_vocab_size if batch['oov_dict'] else None

    if criterion is None:
      target_tensor = None
    else:
      target_tensor = batch['targets']

    out = network(batch, target_tensor, criterion, ext_vocab_size=ext_vocab_size, include_cover_loss=show_cover_loss)
    decoded_batch = batch_decoded_index2word(out.decoded_tokens, vocab, batch['oov_dict'])
  return decoded_batch, out

def batch_decoded_index2word(decoded_tokens, vocab, oov_dict):
  """Convert word indices to strings."""
  decoded_batch = []
  if not isinstance(decoded_tokens, list):
    decoded_tokens = decoded_tokens.transpose(0, 1).tolist()
  for i, doc in enumerate(decoded_tokens):
    decoded_doc = []
    for word_idx in doc:
      if word_idx == vocab.SOS:
        continue
      if word_idx == vocab.EOS:
        break

      if word_idx >= len(vocab):
        word = oov_dict.index2word.get((i, word_idx), vocab.unk_token)
        word = ' '.join(word)
      else:
        word = vocab.getWord(word_idx)
      decoded_doc.append(word)
    decoded_batch.append(' '.join(decoded_doc))
  return decoded_batch

def beam_search(batch, network, vocab, config):
    with torch.no_grad():
        ext_vocab_size = batch['oov_dict'].ext_vocab_size if batch['oov_dict'] else None

        if config['model_name'] == 'seq2seq':
            hypotheses = seq2seq_batch_beam_search(network, batch, ext_vocab_size,
                                    config['beam_size'], min_out_len=config['min_out_len'],
                                    max_out_len=config['max_out_len'],
                                    len_in_words=config['out_len_in_words'],
                                    block_ngram_repeat=config['block_ngram_repeat'])

        else:
            hypotheses = batch_beam_search(network, batch, ext_vocab_size,
                                            config['beam_size'], min_out_len=config['min_out_len'],
                                            max_out_len=config['max_out_len'],
                                            len_in_words=config['out_len_in_words'],
                                            block_ngram_repeat=config['block_ngram_repeat'])
        try:
            to_decode = [each[0].tokens[1:] for each in hypotheses] # the first token is SOS
        except:
            import pdb;pdb.set_trace()
        decoded_batch = batch_decoded_index2word(to_decode, vocab, batch['oov_dict'])
        return decoded_batch


def batch_beam_search(network, ex, ext_vocab_size=None, beam_size=4, *,
                  min_out_len=1, max_out_len=None, len_in_words=False, block_ngram_repeat=0):
    """
    :param input_tensor: tensor of word indices, (batch size, src seq len); for now, batch size has
                         to be 1
    :param input_lengths: see explanation in `EncoderRNN`
    :param ext_vocab_size: see explanation in `DecoderRNN`
    :param beam_size: the beam size
    :param min_out_len: required minimum output length
    :param max_out_len: required maximum output length (if None, use the model's own value)
    :param len_in_words: if True, count output length in words instead of tokens (i.e. do not count
                         punctuations)
    :return: list of the best decoded sequences, in descending order of probability

    Use beam search to generate summaries.
    """

    input_graphs = ex['in_graphs']

    batch_size, max_num_nodes = input_graphs['node_name_words'].shape[:2]
    max_num_edges = input_graphs['edge_type_words'].shape[1]
    num_nodes = input_graphs['num_nodes']
    num_edges = input_graphs['num_edges']


    if network.levi_graph:
      max_num_graph_elements = input_graphs['max_num_graph_nodes']
      num_virtual_nodes = num_nodes + num_edges
      input_mask = create_mask(num_virtual_nodes, max_num_graph_elements, network.device)
      input_node_mask = create_mask(num_nodes, max_num_graph_elements, network.device)

    else:
      # max_num_graph_elements = input_graphs['max_num_graph_nodes']
      input_mask = create_mask(num_nodes, max_num_nodes, network.device)
      input_node_mask = create_mask(num_nodes, max_num_nodes, network.device)


    if max_out_len is None:
      max_out_len = network.max_dec_steps - 1  # max_out_len doesn't count EOS


    if network.use_word_emb:
      # Node name
      node_name_word_emb = network.word_embed(network.filter_oov(input_graphs['node_name_words'], ext_vocab_size))
      node_name_word_emb = dropout(node_name_word_emb, network.word_dropout, shared_axes=[-2], training=network.training)
      node_name_lens = input_graphs['node_name_lens'].view(-1)


      input_node_name_cat = [node_name_word_emb]
      # Answer alignment
      if network.f_ans:
        answer_tensor = ex['answers'] # Shape: (batch_size, max_num_answers, L)
        answer_lens = ex['answer_lens'].view(-1)
        # Shape: (batch_size, max_num_answers * L)
        ans_mask = create_mask(ex['answer_lens'], answer_tensor.size(-1), network.device).view(answer_tensor.size(0), -1)

        ans_emb = network.word_embed(network.filter_oov(answer_tensor, ext_vocab_size))
        ans_emb = dropout(ans_emb, network.word_dropout, shared_axes=[-2], training=network.training)
        # Shape: (batch_size, max_num_answers * L, dim)
        ans_emb = ans_emb.view(ans_emb.size(0), -1, ans_emb.size(-1))

        # Shape: (batch_size, max_num_nodes * L, dim)
        node_name_word_emb = node_name_word_emb.view(node_name_word_emb.size(0), -1, node_name_word_emb.size(-1))


        # Word level alignment
        if network.dan_type in ('all', 'word'):
          # Shape: (batch_size, max_num_nodes, L, dim)
          node_aware_ans_emb = network.node2ans_attn(node_name_word_emb, ans_emb, ans_emb, ans_mask).view(input_graphs['node_name_words'].shape + (-1,))
          input_node_name_cat.append(node_aware_ans_emb)


      input_node_name_cat = torch.cat(input_node_name_cat, -1)
      input_node_name_cat = input_node_name_cat.view(-1, input_node_name_cat.size(-2), input_node_name_cat.size(-1))
      node_name_word_vec = network.node_name_word_encoder(input_node_name_cat, node_name_lens)[1]

      if network.rnn_type == 'lstm':
        node_name_word_vec = node_name_word_vec[0]

      # Shape: (batch_size, max_num_nodes, dim)
      node_name_word_vec = node_name_word_vec.squeeze(0).view(input_graphs['node_name_words'].shape[:2] + (-1,))
      input_node_cat = [node_name_word_vec]
    else:
      input_node_cat = []

    # Hidden level alignment
    if network.f_ans and network.dan_type in ('all', 'hidden'):
      ans_len = ex['answer_lens'].view(-1)
      ans_mask = create_mask(ex['num_answers'], answer_tensor.size(1), network.device)
      ans_vec = network.answer_encoder(ans_emb.view(answer_tensor.size(0) * answer_tensor.size(1), answer_tensor.size(2), -1), ans_len)[1]

      if network.rnn_type == 'lstm':
        ans_vec = ans_vec[0]

      ans_vec = ans_vec.squeeze(0).view(answer_tensor.shape[:2] + (-1,))
      node_aware_ans_emb = network.node2ans_attn_l2(node_name_word_vec, ans_vec, ans_vec, ans_mask)
      input_node_cat.append(node_aware_ans_emb)




    # # Node type
    # node_type_word_emb = network.word_embed(network.filter_oov(input_graphs['node_type_words'], ext_vocab_size))
    # node_type_word_emb = dropout(node_type_word_emb, network.word_dropout, shared_axes=[-2], training=network.training)
    # node_type_word_emb = node_type_word_emb.view(-1, node_type_word_emb.size(-2), node_type_word_emb.size(-1))
    # node_type_lens = input_graphs['node_type_lens'].view(-1)

    # node_type_word_emb = network.node_type_word_encoder(node_type_word_emb, node_type_lens)[1]
    # if network.rnn_type == 'lstm':
    #     node_type_word_emb = node_type_word_emb[0]

    # node_type_word_emb = node_type_word_emb.squeeze(0).view(batch_size, max_num_nodes, -1) # (batch_size, max_num_nodes, hidden_size)

    if network.use_word_emb:
      # Edge type
      edge_type_word_emb = network.word_embed(network.filter_oov(input_graphs['edge_type_words'], ext_vocab_size))
      edge_type_word_emb = dropout(edge_type_word_emb, network.word_dropout, shared_axes=[-2], training=network.training)
      edge_type_word_emb = edge_type_word_emb.view(-1, edge_type_word_emb.size(-2), edge_type_word_emb.size(-1))
      edge_type_lens = input_graphs['edge_type_lens'].view(-1)

      edge_type_word_emb = network.edge_type_word_encoder(edge_type_word_emb, edge_type_lens)[1]
      if network.rnn_type == 'lstm':
        edge_type_word_emb = edge_type_word_emb[0]

      edge_type_word_emb = edge_type_word_emb.squeeze(0).view(batch_size, max_num_edges, -1) # (batch_size, max_num_edges, hidden_size)

      # Fuse node & edge info
      input_edge_cat = [edge_type_word_emb]
    else:
      input_edge_cat = []


    if network.kg_emb:
      node_emb = network.node_embed(input_graphs['node_ids'])
      input_node_cat.append(node_emb)

      edge_type_emb = network.edge_type_embed(input_graphs['edge_type_ids'])
      input_edge_cat.append(edge_type_emb)

    # if network.f_node_type:
    #   node_type_emb = network.node_type_embed(input_graphs['node_type_ids'])
    #   input_node_cat.extend([node_type_emb])



    if network.f_ans_match:
      node_ans_match_emb = network.ans_match_embed(input_graphs['node_ans_match'])
      input_node_cat.append(node_ans_match_emb)
      input_edge_cat.append(to_cuda(torch.zeros(edge_type_word_emb.shape[:2] + (node_ans_match_emb.size(-1),)), network.device))


    input_node_vec = torch.cat(input_node_cat, -1)
    # if len(input_node_cat) > 1:
    #   input_node_vec = network.linear_node_fusion(input_node_vec)

    input_edge_vec = torch.cat(input_edge_cat, -1)
    # if len(input_edge_cat) > 1:
    #   input_edge_vec = network.linear_edge_fusion(input_edge_vec)


    if network.levi_graph:
      # Regard edges as nodes
      init_node_vec = network.gather(input_node_vec, input_edge_vec, num_nodes, num_edges, max_num_graph_elements)
      init_edge_vec = None
    else:
      init_node_vec = input_node_vec
      init_edge_vec = input_edge_vec


    if network.name == 'Graph2Seq':
      node_embedding, graph_embedding = network.graph_encoder(init_node_vec, \
                  init_edge_vec, (input_graphs['node2edge'], input_graphs['edge2node']), \
                  node_mask=input_mask, ans_state=None)
      encoder_outputs = node_embedding
      encoder_state = (graph_embedding, graph_embedding) if network.rnn_type == 'lstm' else graph_embedding

    if network.enc_dec_adapter is None:
      decoder_state = encoder_state
    else:
      if network.rnn_type == 'lstm':
        decoder_state = tuple([network.enc_dec_adapter[i](x) for i, x in enumerate(encoder_state)])
      else:
        decoder_state = network.enc_dec_adapter(encoder_state)


    # Beam search decoding
    batch_results = []
    for batch_idx in range(batch_size):
      # turn batch size from 1 to beam size (by repeating)
      # if we want dynamic batch size, the following must be created for all possible batch sizes
      single_encoder_outputs = encoder_outputs[:, batch_idx: batch_idx + 1].expand(-1, beam_size, -1).contiguous()
      single_input_tensor = input_graphs['g_oov_idx'][batch_idx: batch_idx + 1].expand(beam_size, -1).contiguous() if network.pointer else None
      single_input_mask = input_mask[batch_idx: batch_idx + 1].expand(beam_size, -1).contiguous()
      single_input_node_mask = input_node_mask[batch_idx: batch_idx + 1].expand(beam_size, -1).contiguous()
      single_decoder_state = tuple([each[:, batch_idx: batch_idx + 1] for each in decoder_state]) \
                if network.rnn_type == 'lstm' else decoder_state[:, batch_idx: batch_idx + 1]


      # decode
      hypos = [Hypothesis([network.word_vocab.SOS], [], single_decoder_state, [], [], 1, network.rnn_type)]
      results, backup_results = [], []
      enc_context = None
      step = 0
      # while hypos and step < 2 * max_out_len:  # prevent infinitely generating punctuations
      while len(hypos) > 0 and step <= max_out_len:
        # make batch size equal to beam size (n_hypos <= beam size)
        n_hypos = len(hypos)
        if n_hypos < beam_size:
          hypos.extend(hypos[-1] for _ in range(beam_size - n_hypos))
        # assemble existing hypotheses into a batch
        decoder_input = to_cuda(torch.tensor([h.tokens[-1] for h in hypos]), network.device)
        if network.rnn_type == 'lstm':
            single_decoder_state = (torch.cat([h.dec_state[0] for h in hypos], 1), torch.cat([h.dec_state[1] for h in hypos], 1))
        else:
            single_decoder_state = torch.cat([h.dec_state for h in hypos], 1)
        if network.dec_attn and step > 0:  # dim 0 is decoding step, dim 1 is beam batch
          decoder_hiddens = torch.cat([torch.cat(h.dec_hiddens, 0) for h in hypos], 1)
        else:
          decoder_hiddens = None
        if network.enc_attn_cover:
          enc_attn_weights = [torch.cat([h.enc_attn_weights[i] for h in hypos], 1)
                              for i in range(step)]
        else:
          enc_attn_weights = []
        if enc_attn_weights:
          coverage_vector = network.get_coverage_vector(enc_attn_weights)  # shape: (beam size, src len)
        else:
          coverage_vector = None
        # run the decoder over the assembled batch
        decoder_embedded = network.word_embed(network.filter_oov(decoder_input, ext_vocab_size))
        decoder_output, single_decoder_state, dec_enc_attn, dec_prob_ptr, enc_context = \
          network.decoder(decoder_embedded, single_decoder_state, single_encoder_outputs,
                       decoder_hiddens, coverage_vector,
                       input_mask=single_input_mask,
                       input_node_mask=single_input_node_mask,
                       encoder_word_idx=single_input_tensor, ext_vocab_size=ext_vocab_size,
                       prev_enc_context=enc_context)
        top_v, top_i = decoder_output.data.topk(beam_size)  # shape of both: (beam size, beam size)
        # create new hypotheses
        new_hypos = []
        for in_idx in range(n_hypos):
          for out_idx in range(beam_size):
            new_tok = top_i[in_idx][out_idx].item()
            new_prob = top_v[in_idx][out_idx].item()
            if len_in_words:
              non_word = not network.word_vocab.is_word(new_tok)
            else:
              non_word = new_tok == network.word_vocab.EOS  # only SOS & EOS don't count

            if network.rnn_type == 'lstm':
              tmp_decoder_state = [x[0][in_idx].unsqueeze(0).unsqueeze(0) for x in single_decoder_state]
            else:
              tmp_decoder_state = single_decoder_state[0][in_idx].unsqueeze(0).unsqueeze(0)
            new_hypo = hypos[in_idx].create_next(new_tok, new_prob,
                                                 tmp_decoder_state,
                                                 network.dec_attn,
                                                 dec_enc_attn[in_idx].unsqueeze(0).unsqueeze(0)
                                                 if dec_enc_attn is not None else None, non_word)
            new_hypos.append(new_hypo)

        # Block sequences with repeated ngrams
        block_ngram_repeats(new_hypos, block_ngram_repeat)

        # process the new hypotheses
        new_hypos = sorted(new_hypos, key=lambda h: -h.avg_log_prob)[:beam_size]
        hypos = []
        new_complete_results, new_incomplete_results = [], []
        for nh in new_hypos:
          length = len(nh) # Does not count SOS and EOS
          if nh.tokens[-1] == network.word_vocab.EOS:  # a complete hypothesis
            if len(new_complete_results) < beam_size and min_out_len <= length <= max_out_len:
              new_complete_results.append(nh)
          elif len(hypos) < beam_size and length < max_out_len:  # an incomplete hypothesis
            hypos.append(nh)
          elif length == max_out_len and len(new_incomplete_results) < beam_size:
            new_incomplete_results.append(nh)
        if new_complete_results:
          results.extend(new_complete_results)
        elif new_incomplete_results:
          backup_results.extend(new_incomplete_results)
        step += 1
      if not results:  # if no sequence ends with EOS within desired length, fallback to sequences
        results = backup_results  # that are "truncated" at the end to max_out_len
      batch_results.append(sorted(results, key=lambda h: -h.avg_log_prob)[:beam_size])
    return batch_results



def block_ngram_repeats(hypos, block_ngram_repeat, exclusion_tokens=set()):
    cur_len = len(hypos[0].tokens)
    if block_ngram_repeat > 0 and cur_len > 1:
        for path_idx in range(len(hypos)):
            # skip SOS
            hyp = hypos[path_idx].tokens[1:]
            ngrams = set()
            fail = False
            gram = []
            for i in range(cur_len - 1):
                # Last n tokens, n = block_ngram_repeat
                gram = (gram + [hyp[i]])[-block_ngram_repeat:]
                # skip the blocking if any token in gram is excluded
                if set(gram) & exclusion_tokens:
                    continue
                if tuple(gram) in ngrams:
                    fail = True
                ngrams.add(tuple(gram))
            if fail:
                hypos[path_idx].log_probs[-1] = -INF

class Hypothesis(object):
  def __init__(self, tokens, log_probs, dec_state, dec_hiddens, enc_attn_weights, num_non_words, rnn_type):
    self.tokens = tokens  # type: List[int]
    self.log_probs = log_probs  # type: List[float]
    self.dec_state = dec_state  # shape: (1, 1, hidden_size)
    self.dec_hiddens = dec_hiddens  # list of dec_hidden_state
    self.enc_attn_weights = enc_attn_weights  # list of shape: (1, 1, src_len)
    self.num_non_words = num_non_words  # type: int
    self.rnn_type = rnn_type

  def __repr__(self):
    return repr(self.tokens)

  def __len__(self):
    return len(self.tokens) - self.num_non_words

  @property
  def avg_log_prob(self):
    return sum(self.log_probs) / len(self.log_probs)

  def create_next(self, token, log_prob, dec_state, add_dec_states, enc_attn, non_word):
    dec_hidden_state = dec_state[0] if self.rnn_type == 'lstm' else dec_state
    return Hypothesis(tokens=self.tokens + [token], log_probs=self.log_probs + [log_prob],
                      dec_state=dec_state, dec_hiddens=
                      self.dec_hiddens + [dec_hidden_state] if add_dec_states else self.dec_hiddens,
                      enc_attn_weights=self.enc_attn_weights + [enc_attn]
                      if enc_attn is not None else self.enc_attn_weights,
                      num_non_words=self.num_non_words + 1 if non_word else self.num_non_words,
                      rnn_type=self.rnn_type)

def evaluate_predictions(target_src, decoded_text):
    assert len(target_src) == len(decoded_text)
    eval_targets = {}
    eval_predictions = {}
    for idx in range(len(target_src)):
        eval_targets[idx] = [target_src[idx]]
        eval_predictions[idx] = [decoded_text[idx]]

    QGEval = QGEvalCap(eval_targets, eval_predictions)
    scores = QGEval.evaluate()
    return scores



def seq2seq_batch_beam_search(network, ex, ext_vocab_size=None, beam_size=4, *,
                  min_out_len=1, max_out_len=None, len_in_words=False, block_ngram_repeat=0):
    """
    :param input_tensor: tensor of word indices, (batch size, src seq len); for now, batch size has
                         to be 1
    :param input_lengths: see explanation in `EncoderRNN`
    :param ext_vocab_size: see explanation in `DecoderRNN`
    :param beam_size: the beam size
    :param min_out_len: required minimum output length
    :param max_out_len: required maximum output length (if None, use the model's own value)
    :param len_in_words: if True, count output length in words instead of tokens (i.e. do not count
                         punctuations)
    :return: list of the best decoded sequences, in descending order of probability

    Use beam search to generate summaries.
    """

    batch_size, input_length = ex['src_seqs'].shape[:2]
    input_seq_lens = ex['src_lens']
    input_mask = create_mask(input_seq_lens, input_length, network.device)

    if max_out_len is None:
      max_out_len = network.max_dec_steps - 1  # max_out_len doesn't count EOS

    input_seq_emb = network.word_embed(network.filter_oov(ex['src_seqs'], ext_vocab_size))
    input_seq_emb = dropout(input_seq_emb, network.word_dropout, shared_axes=[-2], training=network.training)
    encoder_outputs, encoder_state = network.seq_encoder(input_seq_emb, input_seq_lens)


    if network.enc_dec_adapter is None:
      decoder_state = encoder_state
    else:
      if network.rnn_type == 'lstm':
        decoder_state = tuple([network.enc_dec_adapter[i](x) for i, x in enumerate(encoder_state)])
      else:
        decoder_state = network.enc_dec_adapter(encoder_state)


    # Beam search decoding
    batch_results = []
    for batch_idx in range(batch_size):
      # turn batch size from 1 to beam size (by repeating)
      # if we want dynamic batch size, the following must be created for all possible batch sizes
      single_encoder_outputs = encoder_outputs[:, batch_idx: batch_idx + 1].expand(-1, beam_size, -1).contiguous()
      single_input_tensor = ex['src_seqs'][batch_idx: batch_idx + 1].expand(beam_size, -1).contiguous()
      single_input_mask = input_mask[batch_idx: batch_idx + 1].expand(beam_size, -1).contiguous()
      single_decoder_state = tuple([each[:, batch_idx: batch_idx + 1] for each in decoder_state]) \
                if network.rnn_type == 'lstm' else decoder_state[:, batch_idx: batch_idx + 1]


      # decode
      hypos = [Hypothesis([network.word_vocab.SOS], [], single_decoder_state, [], [], 1, network.rnn_type)]
      results, backup_results = [], []
      enc_context = None
      step = 0
      # while hypos and step < 2 * max_out_len:  # prevent infinitely generating punctuations
      while len(hypos) > 0 and step <= max_out_len:
        # make batch size equal to beam size (n_hypos <= beam size)
        n_hypos = len(hypos)
        if n_hypos < beam_size:
          hypos.extend(hypos[-1] for _ in range(beam_size - n_hypos))
        # assemble existing hypotheses into a batch
        decoder_input = to_cuda(torch.tensor([h.tokens[-1] for h in hypos]), network.device)
        if network.rnn_type == 'lstm':
            single_decoder_state = (torch.cat([h.dec_state[0] for h in hypos], 1), torch.cat([h.dec_state[1] for h in hypos], 1))
        else:
            single_decoder_state = torch.cat([h.dec_state for h in hypos], 1)
        if network.dec_attn and step > 0:  # dim 0 is decoding step, dim 1 is beam batch
          decoder_hiddens = torch.cat([torch.cat(h.dec_hiddens, 0) for h in hypos], 1)
        else:
          decoder_hiddens = None
        if network.enc_attn_cover:
          enc_attn_weights = [torch.cat([h.enc_attn_weights[i] for h in hypos], 1)
                              for i in range(step)]
        else:
          enc_attn_weights = []
        if enc_attn_weights:
          coverage_vector = network.get_coverage_vector(enc_attn_weights)  # shape: (beam size, src len)
        else:
          coverage_vector = None
        # run the decoder over the assembled batch
        decoder_embedded = network.word_embed(network.filter_oov(decoder_input, ext_vocab_size))
        decoder_output, single_decoder_state, dec_enc_attn, dec_prob_ptr, enc_context = \
          network.decoder(decoder_embedded, single_decoder_state, single_encoder_outputs,
                       decoder_hiddens, coverage_vector,
                       input_mask=single_input_mask,
                       input_node_mask=single_input_mask,
                       encoder_word_idx=single_input_tensor, ext_vocab_size=ext_vocab_size,
                       prev_enc_context=enc_context)
        top_v, top_i = decoder_output.data.topk(beam_size)  # shape of both: (beam size, beam size)
        # create new hypotheses
        new_hypos = []
        for in_idx in range(n_hypos):
          for out_idx in range(beam_size):
            new_tok = top_i[in_idx][out_idx].item()
            new_prob = top_v[in_idx][out_idx].item()
            if len_in_words:
              non_word = not network.word_vocab.is_word(new_tok)
            else:
              non_word = new_tok == network.word_vocab.EOS  # only SOS & EOS don't count

            if network.rnn_type == 'lstm':
              tmp_decoder_state = [x[0][in_idx].unsqueeze(0).unsqueeze(0) for x in single_decoder_state]
            else:
              tmp_decoder_state = single_decoder_state[0][in_idx].unsqueeze(0).unsqueeze(0)
            new_hypo = hypos[in_idx].create_next(new_tok, new_prob,
                                                 tmp_decoder_state,
                                                 network.dec_attn,
                                                 dec_enc_attn[in_idx].unsqueeze(0).unsqueeze(0)
                                                 if dec_enc_attn is not None else None, non_word)
            new_hypos.append(new_hypo)

        # Block sequences with repeated ngrams
        block_ngram_repeats(new_hypos, block_ngram_repeat)

        # process the new hypotheses
        new_hypos = sorted(new_hypos, key=lambda h: -h.avg_log_prob)[:beam_size]
        hypos = []
        new_complete_results, new_incomplete_results = [], []
        for nh in new_hypos:
          length = len(nh) # Does not count SOS and EOS
          if nh.tokens[-1] == network.word_vocab.EOS:  # a complete hypothesis
            if len(new_complete_results) < beam_size and min_out_len <= length <= max_out_len:
              new_complete_results.append(nh)
          elif len(hypos) < beam_size and length < max_out_len:  # an incomplete hypothesis
            hypos.append(nh)
          elif length == max_out_len and len(new_incomplete_results) < beam_size:
            new_incomplete_results.append(nh)
        if new_complete_results:
          results.extend(new_complete_results)
        elif new_incomplete_results:
          backup_results.extend(new_incomplete_results)
        step += 1
      if not results:  # if no sequence ends with EOS within desired length, fallback to sequences
        results = backup_results  # that are "truncated" at the end to max_out_len
      batch_results.append(sorted(results, key=lambda h: -h.avg_log_prob)[:beam_size])
    return batch_results
