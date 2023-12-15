from typing import Any, Callable, List, MutableMapping, Union, Optional

import torch
import torch.nn.functional as F

from fms.modules.speculator import Speculator


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
    paged_kv_cache: Optional["PagedKVCache"] = None,  # type: ignore
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
    kwargs["use_cache"] = use_cache

    if use_cache:
        if paged_kv_cache:
            sequence_ids = paged_kv_cache.get_unassigned_sequence_ids(input_ids.size(0))
            kwargs["past_key_value_states"] = paged_kv_cache.cache
        else:
            kwargs["past_key_value_states"] = None

    for i in range(max_new_tokens):

        input_ids = next_input[:, -max_seq_len:]

        # cache allocation
        if use_cache:
            if paged_kv_cache:
                # this is the prompt
                if i == 0:
                    kwargs["cache_metadata"] = paged_kv_cache.allocate_initial_prompt(
                        input_ids, sequence_ids
                    )
                    # todo: need to make the mask something generic for generate, but keeping here for now for testing
                    #  currently we make an assumption that the pad token is 0
                    is_pad = input_ids == 0
                    mask = is_pad.unsqueeze(-1) == is_pad.unsqueeze(-2)
                    mask = mask.tril(diagonal=0)
                    kwargs["mask"] = mask
                else:
                    kwargs["cache_metadata"] = paged_kv_cache.allocate_generated_token(
                        sequence_ids, 1
                    )
                    kwargs["mask"] = None

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

    if use_cache and paged_kv_cache:
        paged_kv_cache.free_sequences(sequence_ids)

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
    input_ids: Union[torch.Tensor, List[torch.Tensor]],
    speculator: Speculator,
    max_seq_len: int = 2048,
    new_tokens: int = 256,
    top_k: int = 5,
    threshes=[5, 3, 2],
    verbose_dict=None,
    paged_kv_cache: Optional["PagedKVCache"] = None,  # type: ignore
):
    """
    A reference implementation of speculative decoding generation.
    Returns at least the specified number of tokens - the speculator may return a
    few extra in the final step.
    If input is batched, continues generating until EVERY sequence has produced AT LEAST the required number of tokens.
    Input (and output) tokens beyond max_seq_len are simply dropped for a sliding-window approach.
    Currently reproduces behavior of greedy decoding only.
    Args:
        model: A function or nn.Module that takes a batch of input_ids and
            returns logits
        input_ids: A length n tensor of token IDs, or list of such tensors
        speculator: A function or nn.Module that takes a state vector and sampled token
            and returns a set of candidate suffixes
        max_seq_len: the sequence length of the base model
        new_tokens: number of tokens to generate
        top_k: only score the top k candidates from the speculator
        threshes: use top k predictions from each head to generate speculator candidate pool
        verbose_dict: Optional HF tokenizer vocab dict. If provided, runs verbosely and prints
            speculator behavior and scoring for each step
    Returns:
        result: List of id tensors, possibly different lengths if batching.
        n_steps: Number of foward passes used to generate provided tokens.
    """

    verbose = False
    if verbose_dict is not None:
        verbose = True
        vinv = {v: k for k, v in verbose_dict.items()}

    def decode_obo(x, vinv):
        return [vinv[z] for z in x.squeeze().tolist()]

    # Construct batch(es) and initial inputs
    bsize = len(input_ids)
    result = input_ids  # [b] n
    # Build padded batched input tensor
    max_len = max([seq.size(0) for seq in input_ids])
    n_pads_init = [max_len - seq.size(0) for seq in input_ids]
    n_pads = torch.Tensor(n_pads_init).to(device=input_ids[0].device, dtype=torch.int)
    inputs = torch.stack(
        [F.pad(input_ids[i], (n_pads_init[i], 0)) for i in range(bsize)]
    )
    cache_metadata = paged_kv_cache.allocate_initial_prompt(inputs[:, :-1])
    parent_sequence_ids = cache_metadata['sequence_ids']
    # Build padded causal mask
    mask = torch.ones(
        bsize,
        1,
        inputs.size(1) - 1,
        inputs.size(1) - 1,
        device=inputs.device,
    )
    mask = mask.tril()  # b 1 n-1 n-1
    # Mask off any left-pads
    pad_mask = torch.arange(mask.size(3), device=mask.device).view(
        1, 1, 1, -1
    )  # 1 1 1 n-1
    pad_mask = pad_mask.expand(bsize, 1, 1, -1)  # b 1 1 n-1
    pad_mask = pad_mask.sub(n_pads.sub(1).view(-1, 1, 1, 1)).clamp(0, 1)
    eye = torch.eye(mask.size(3), device=mask.device)[None, None, :, :]  # 1 1 n-1 n-1
    mask = mask.mul(pad_mask).logical_or(eye).log()  # b 1 n-1 n-1
    # Handle position_ids
    pos_ids = torch.arange(mask.size(3), device=inputs.device).repeat(bsize, 1)  # b n-1
    pos_ids -= n_pads[:, None]

    kwargs: MutableMapping[str, Any] = dict()
    kwargs["past_key_value_states"] = None if not paged_kv_cache else paged_kv_cache.cache
    kwargs["use_cache"] = True

    # Build kv cache and get initial state vector
    n_adds = speculator.n_predict + 1
    inputs = inputs[:, -max_seq_len + n_adds :]
    output = model(
        inputs[:, :-1],
        include_embeds=True,
        position_ids=cache_metadata['position_offset'],
        mask=mask,
        cache_metadata=cache_metadata,
        **kwargs
    )
    _, past_key_value_states, embeds = output
    embeds = embeds[:, -1:]
    kwargs["past_key_value_states"] = past_key_value_states

    n_gen = torch.zeros(bsize, device=inputs.device, dtype=torch.int)
    n_steps = 0
    inputs = inputs[:, -1:]
    while min(n_gen) < new_tokens:
        n_steps += 1

        # create candidate sequences
        child_sequence_ids_list = []
        child_sequence_ids_flattened = []
        # each parent will have top_k child sequences
        for parent_sequence_id in parent_sequence_ids:
            child_sequence_ids = paged_kv_cache.add_child_sequences(parent_sequence_id, top_k)
            child_sequence_ids_list.append(child_sequence_ids)
            child_sequence_ids_flattened.extend(child_sequence_ids)

        # add n_adds tokens to each candidate
        cache_metadata = paged_kv_cache.allocate_generated_token(child_sequence_ids_flattened, n_adds)

        # Get candidate set of speculations
        adds = speculator.generate_suffixes(embeds, inputs, threshes, top_k).transpose(
            0, 1
        )  # k b h
        inputs = torch.cat(
            [inputs.unsqueeze(0).expand(top_k, bsize, 1), adds], dim=-1
        ).int()  # k b 1+h
        inputs = inputs.view(-1, n_adds)  # kb 1+h
        # Base model forward pass
        output = model(
            inputs, include_embeds=True, cache_metadata=cache_metadata, position_ids=cache_metadata['position_offset'], **kwargs
        )
        logits, past_key_value_states, embeds = output
        next_vals = torch.argmax(logits, dim=-1)  # kb 1+h

        # Check correctness of speculator predictions
        test = inputs.roll(-1, 1).eq(next_vals).cumprod(1)
        n_correct = (
            test.sum(1).clamp(0, n_adds - 1).view(top_k, bsize)
        )  # clamp in case pred[0]==targ[-1]
        best_guess = n_correct.argmax(0)  # b
        best_guess_unflat = (
            best_guess.unsqueeze(1).expand(bsize, n_adds).unsqueeze(0)
        )  # 1 b 1+h

        # Set global values to those of best guess
        next_vals = next_vals.view(top_k, bsize, n_adds).gather(0, best_guess_unflat)[
            0
        ]  # b 1+h
        n_correct = n_correct.gather(0, best_guess.unsqueeze(0))[0]  # b
        embeds = embeds.view(top_k, bsize, *embeds.size()[1:]).gather(
            0, best_guess_unflat.unsqueeze(3).expand(-1, -1, -1, embeds.size(2))
        )[
            0
        ]  # b 1+h d

        if verbose:
            test = inputs.view(top_k, bsize, n_adds).gather(0, best_guess_unflat)[0]
            for i, line in enumerate(test):
                print(
                    "Speculation:",
                    decode_obo(line, vinv),
                    "n_correct:",
                    n_correct[i].item(),
                )

        # free all worst candidates and keep best candidates as parents
        parent_sequence_ids = []
        for parent_index, child_sequence_ids in enumerate(child_sequence_ids_list):
            best_index = best_guess[parent_index].item()

            # free all bad candidates
            paged_kv_cache.free_sequences(child_sequence_ids[:best_index] + child_sequence_ids[best_index + 1:])

            # decrease the context length of the sequence which used to be sequence length + n_adds by the number of incorrect tokens
            # for the correct candidate
            best_sequence_id = child_sequence_ids[best_index]
            parent_sequence_ids.append(best_sequence_id)
            paged_kv_cache.remove_tokens(best_sequence_id, n_adds - n_correct[parent_index].item() - 1)

        # Toss any wrong speculator tokens
        next_vals_split = list(next_vals)
        next_vals_split = [
            next_vals_split[i][: n_correct[i] + 1] for i in range(len(next_vals_split))
        ]  # [b] h'
        n_gen += n_correct + 1
        embeds = embeds.gather(
            1, n_correct.view(-1, 1, 1).expand(-1, -1, embeds.size(2))
        )  # Grab last correct embed
        kwargs["past_key_value_states"] = past_key_value_states

        # Update results
        result = [
            torch.cat((result[i], next_vals_split[i]), dim=0) for i in range(bsize)
        ]
        inputs = torch.stack([line[-1:] for line in next_vals_split], dim=0)  # b 1

        if verbose:
            for line in result:
                print("Updated output:", decode_obo(line, vinv))
            print()

    return result, n_steps
