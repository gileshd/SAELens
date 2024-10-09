import torch
from mamba_lens import HookedMamba
from transformer_lens import HookedTransformer
from transformers import AutoModelForCausalLM, AutoTokenizer

from sae_lens.load_model import HookedProxyLM, load_model


def test_load_model_works_with_mamba():
    model = load_model(
        model_class_name="HookedMamba",
        model_name="state-spaces/mamba-370m",
        device="cpu",
    )
    assert model is not None
    assert isinstance(model, HookedMamba)


def test_load_model_works_without_model_kwargs():
    model = load_model(
        model_class_name="HookedTransformer",
        model_name="pythia-14m",
        device="cpu",
    )
    assert model is not None
    assert isinstance(model, HookedTransformer)
    assert model.cfg.checkpoint_index is None


def test_load_model_works_with_model_kwargs():
    model = load_model(
        model_class_name="HookedTransformer",
        model_name="pythia-14m",
        device="cpu",
        model_from_pretrained_kwargs={"checkpoint_index": 0},
    )
    assert model is not None
    assert isinstance(model, HookedTransformer)
    assert model.cfg.checkpoint_index == 0


def test_load_model_with_generic_huggingface_lm():
    model = load_model(
        model_class_name="AutoModelForCausalLM",
        model_name="gpt2",
        device="cpu",
    )
    assert model is not None
    assert isinstance(model, HookedProxyLM)


def test_HookedProxyLM_gives_same_cached_states_as_original_implementation():
    hf_model = AutoModelForCausalLM.from_pretrained("gpt2")
    tokenizer = AutoTokenizer.from_pretrained("gpt2")
    hooked_model = HookedProxyLM(hf_model, tokenizer)
    input_ids = tokenizer.encode("hi", return_tensors="pt")
    output, cache = hooked_model.run_with_cache(input_ids)

    hf_output = hf_model(input_ids, output_hidden_states=True)

    assert torch.allclose(output.logits, hf_output.logits)
    for i in range(len(hf_output.hidden_states) - 2):
        assert torch.allclose(
            cache[f"transformer.h.{i}"], hf_output.hidden_states[i + 1]
        )


def test_HookedProxyLM_forward_gives_same_output_as_tlens():
    hf_model = load_model(
        model_class_name="AutoModelForCausalLM",
        model_name="gpt2",
        device="cpu",
    )
    tlens_model = HookedTransformer.from_pretrained("gpt2", device="cpu")

    batch_tokens = tlens_model.to_tokens("hi there")
    tlens_output = tlens_model(batch_tokens, return_type="both", loss_per_token=True)
    hf_output = hf_model(batch_tokens, return_type="both", loss_per_token=True)

    # Seems like tlens removes the means before softmaxing
    hf_logits_normed = hf_output[0] - hf_output[0].mean(dim=-1, keepdim=True)

    assert torch.allclose(tlens_output[0], hf_logits_normed, atol=1e-3)
    assert torch.allclose(tlens_output[1], hf_output[1], atol=1e-3)


def test_HookedProxyLM_to_tokens_gives_same_output_as_tlens():
    hf_model = load_model(
        model_class_name="AutoModelForCausalLM",
        model_name="gpt2",
        device="cpu",
    )
    tlens_model = HookedTransformer.from_pretrained("gpt2", device="cpu")

    tl_tokens = tlens_model.to_tokens(
        "hi there", prepend_bos=False, truncate=False, move_to_device=False
    )
    hf_tokens = hf_model.to_tokens(
        "hi there", prepend_bos=False, truncate=False, move_to_device=False
    )

    assert torch.allclose(tl_tokens, hf_tokens)
