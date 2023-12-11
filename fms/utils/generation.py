from typing import Any, Callable, List, MutableMapping, Union

import torch
import torch.nn.functional as F


def _make_cache_contiguous(past_key_value_states):
    # kv updates are required for torch.compile with
    # mode='reduce-overhead'
    n_kv_s: List[List[torch.Tensor]] = []
    for layer_idx in range(len(past_key_value_states)):
        n_kv_s.append([])
        for tensor_idx in range(len(past_key_value_states[layer_idx])):
            n_kv_s[layer_idx].append(
                past_key_value_states[layer_idx][tensor_idx]
                .clone(memory_format=torch.contiguous_format)
                .detach()
            )
            # torch._dynamo.mark_dynamic(n_kv_s[layer_idx][tensor_idx], 2)
    return n_kv_s


def generate(
    model: Union[Callable, torch.nn.Module],
    input_ids: torch.Tensor,
    max_seq_len: int = 2048,
    max_new_tokens: int = 256,
    temperature: float = 1.0,
    top_k: int = 10,
    do_sample: bool = True,
    num_beams: int = 1,
    use_cache: bool = False,
    contiguous_cache: bool = False,
):
    """
    A trivial generate function that can be used for validation/testing in
    cases where HF is not available.
    We could add implementations for other types of generation, but this is
    enough for making sure a model is working.
    Does not implement batching nor beam search, but those could be added.

    Args:
        model: A function or nn.Module that takes a batch of input_ids and
            returns logits
        prefix: A tensor of token IDs.
        max_seq_len: the sequence length of the model
        max_new_tokens: max tokens to generate
        temperature: temperature of softmax when sampling
        top_k: only search among top k tokens
        do_sample: multinomial sampling. False for greedy.
        num_beams: TODO: support beam search
        use_cache: requires that the model accept use_cache and
            past_key_value_states args in forward method.
    """
    batched = False
    if num_beams != 1:
        raise NotImplementedError("generate() does yet not support beam search")
    if type(input_ids) == torch.Tensor:
        if input_ids.dim() != 1:
            batched = True
    else:
        raise RuntimeError("generate() requires a tensor of token ids as the prefix")

    if not batched:
        input_ids = input_ids.unsqueeze(0)

    result = input_ids
    next_input = input_ids
    kwargs: MutableMapping[str, Any] = dict()
    kwargs["past_key_value_states"] = None
    kwargs["use_cache"] = use_cache

    for _ in range(max_new_tokens):
        input_ids = next_input[:, -max_seq_len:]
        output = model(input_ids, **kwargs)
        if use_cache:
            logits, past_key_value_states = output
            # TODO: this should go away when reduce-overhead issues are fixed, or
            # maybe could be moved into model code to be more portable.
            if contiguous_cache:
                kwargs["past_key_value_states"] = _make_cache_contiguous(
                    past_key_value_states
                )
            else:
                kwargs["past_key_value_states"] = past_key_value_states
        else:
            logits = output
        logits = logits[:, -1, :]

        if do_sample:
            # get logits from last value in sequence nad scale
            logits = logits / temperature
            if top_k:
                v, _ = torch.topk(logits, top_k)
                logits[logits < v[:, [-1]]] = -float("inf")

            probs = F.softmax(logits, dim=-1)
            next_val = torch.multinomial(probs, num_samples=1)
        else:
            next_val = torch.argmax(logits, dim=-1).unsqueeze(0).t()

        result = torch.cat((result, next_val), dim=-1)

        if use_cache:
            next_input = next_val
        else:
            next_input = result

    if not batched:
        result = result[0]
    return result


def truncate_after_eos(result, eos_token_id):
    """
    Helper function to return a truncated sequence of token IDs stopping at
    (and including) the 'end of sentence' token.
    Currently only handles unbatched sequences.
    """
    if eos_token_id is None:
        return result

    eos_idx = torch.where(result == eos_token_id)
    eos_idx = eos_idx[0]
    if eos_idx.shape[0] >= 1:
        eos_idx = eos_idx[0].item()
        result = result[: eos_idx + 1]
    return result


def speculative_generate(
    model: Union[Callable, torch.nn.Module],
    input_ids: torch.LongTensor,
    speculator: Union[Callable, torch.nn.Module],
    max_seq_len: int = 2048,
    new_tokens: int = 256,
    top_k: int = 5,
    threshes = [5,3,2],
    verbose_dict = None
):
    """
    A reference implementation of speculative decoding generation.
    Returns at least the specified number of tokens - the speculator may return a 
    few extra in the final step. 
    If input is batched, continues generating until EVERY sequence has produced AT LEAST the required number of tokens. 
    Currently reproduces behavior of greedy decoding only.

    Args:
        model: A function or nn.Module that takes a batch of input_ids and
            returns logits
        input_ids: A bxn or length n tensor of token IDs
        speculator: A function or nn.Module that takes a state vector and sampled token
            and returns a set of candidate suffixes
        max_seq_len: the sequence length of the base model
        new_tokens: number of tokens to generate
        top_k: only score the top k candidates from the speculator
        threshes: use top k predictions from each head to generate speculator candidate pool 
        verbose_dict: Optional HF tokenizer vocab dict. If provided, runs verbosely and prints 
            speculator behavior and scoring for each step
    """

    verbose = False
    if verbose_dict is not None:
        verbose = True
        vinv = {v:k for k,v in verbose_dict.items()}
        
    def decode_obo(x, vinv):
        return [vinv[z] for z in x.squeeze().tolist()]
    
    batched = False
    if type(input_ids) == torch.Tensor:
        if input_ids.dim() != 1:
            batched = True
    else:
        raise RuntimeError("generate() requires a tensor of token ids as the prefix")

    if not batched:
        input_ids = input_ids.unsqueeze(0)

    result = list(input_ids) # [b] n
    kwargs = dict()
    kwargs["past_key_value_states"] = None
    kwargs["use_cache"] = True

    # Build kv cache and get initial state vector
    output = model(input_ids[:,:-1], include_embeds=True, **kwargs)
    _, past_key_value_states, embeds = output
    embeds = embeds[:,-1:]
    kwargs["past_key_value_states"] = past_key_value_states
    
    bsize = input_ids.size(0)
    n_gen = torch.zeros(bsize, device=input_ids.device).int()
    n_steps = 0
    n_kv_s = past_key_value_states
    n_pads = torch.zeros_like(n_gen).int()
    prompt_len = input_ids.size(1)-1
    input_ids = input_ids[:,-1:]
    n_adds = speculator.nheads + 1
    while min(n_gen) < new_tokens:
        n_steps += 1
        
        # Get candidate set of speculations
        adds = speculator.generate_tree(embeds, input_ids, threshes, top_k).transpose(0,1) # k b h
        input_ids = torch.cat([input_ids.unsqueeze(0).expand(top_k,bsize,1), adds], dim=-1) # k b 1+h
        input_ids = input_ids.view(-1, n_adds) # kb 1+h

        # Build custom attention mask
        mask = torch.ones(input_ids.size(1),input_ids.size(1)+n_kv_s[0][0].size(2), device=input_ids.device)
        mask = mask.tril(diagonal=mask.size(1)-mask.size(0))
        mask = mask.unsqueeze(0).unsqueeze(0) # 1 1 1+h 1+h+p
        
        # Mask off any left-pads
        pad_mask = mask.repeat(bsize,1,1,1) # b 1 1+h 1+h+p
        pad_mask = pad_mask.cumsum(3).sub(n_pads.view(-1,1,1,1)).clamp(0,1)
        mask = mask.mul(pad_mask).repeat(top_k,1,1,1).log() # kb 1 1+h 1+h+p
        
        # Handle position_ids
        pos_ids = torch.arange(n_adds, device=input_ids.device).repeat(bsize,1) # b 1+h
        pos_ids += prompt_len - n_pads[:,None]
        pos_ids = pos_ids.repeat(top_k, 1) # kb 1+h
        
        # Base model forward pass
        output = model.forward(input_ids, include_embeds=True, mask=mask, position_ids=pos_ids, **kwargs)
        logits, past_key_value_states, embeds = output
        next_vals = torch.argmax(logits, dim=-1) # kb 1+h
        
        # Check correctness of speculator predictions
        test = input_ids.roll(-1, 1).eq(next_vals).cumprod(1)
        n_correct = test.sum(1).clamp(0,n_adds-1).view(top_k, bsize) # clamp in case pred[0]==targ[-1]
        best_guess = n_correct.argmax(0) # b
        best_guess_unflat = best_guess.unsqueeze(1).expand(bsize, n_adds).unsqueeze(0) # 1 b 1+h
        
        # Set global values to those of best guess
        next_vals = next_vals.view(top_k, bsize, n_adds).gather(0, best_guess_unflat)[0] # b 1+h
        n_correct = n_correct.gather(0, best_guess.unsqueeze(0))[0] # b
        embeds = embeds.view(top_k, bsize, *embeds.size()[1:]).gather(
            0, best_guess_unflat.unsqueeze(3).expand(-1,-1,-1,embeds.size(2)))[0] # b 1+h d
        
        if verbose:
            test = input_ids.view(top_k, bsize, n_adds).gather(0, best_guess_unflat)[0]
            for i,line in enumerate(test):
                print("Speculation:", decode_obo(line, vinv), "n_correct:", n_correct[i].item())
        
        
        # Toss any wrong speculator tokens
        next_vals = list(next_vals)
        next_vals = [next_vals[i][:n_correct[i]+1] for i in range(len(next_vals))] # [b] h'
        n_gen += n_correct+1
        embeds = embeds.gather(1, n_correct.view(-1,1,1).expand(-1,-1,embeds.size(2))) # Grab last correct embed
        
        # Handle kv-cache
        n_wrong = n_adds - 1 - n_correct
        n_pads += n_wrong
        extra_pads = min(n_pads)
        prompt_len += n_adds - extra_pads
        n_pads = n_pads-extra_pads
        # kv updates are required for torch.compile with
        # mode='reduce-overhead'
        n_kv_s = []
        for layer_idx in range(len(past_key_value_states)):
            n_kv_s.append([])
            for tensor_idx in range(2):
                # Concatenate best guess for each sequence to kv-cache
                base = past_key_value_states[layer_idx][tensor_idx] # b h n d
                new = past_key_value_states[layer_idx][tensor_idx+2] # kb h n d
                new = new.view(top_k, bsize, *new.size()[1:]) # k b h n d
                g = best_guess[None, :, None, None, None] # 1 b 1 1 1
                new = new.gather(0, g.expand_as(new[:1]))[0] # b h n d
                base = torch.cat([base, new], dim=2) # b h n d
                
                # Right-shift correct tokens to end of cache
                roll_inds = torch.arange(base.size(2))[None, None, :, None] # 1 1 n 1
                roll_inds = roll_inds.repeat(base.size(0), 1, 1, 1) # b 1 n 1
                roll_inds = roll_inds.sub(n_wrong.view(-1,1,1,1)) % roll_inds.size(2) # Right-shift
                roll_inds = roll_inds[:,:,extra_pads:] # Knock off any unneeded left-pads
                base = base.gather(2, roll_inds.expand(-1,base.size(1),-1,base.size(3))) # Perform shift
                
                n_kv_s[layer_idx].append(
                    base.clone(memory_format=torch.contiguous_format).detach()
                )
                # torch._dynamo.mark_dynamic(n_kv_s[layer_idx][tensor_idx], 2)
        kwargs["past_key_value_states"] = n_kv_s

        # Update results
        result = [torch.cat((result[i], next_vals[i]), dim=0) for i in range(bsize)]
        input_ids = torch.stack([line[-1:] for line in next_vals], dim=0) # b 1

        if verbose:
            for line in result:
                print("Updated output:", decode_obo(line, vinv))
            print()
        
    if not batched:
        result = result[0]
    return result, n_steps