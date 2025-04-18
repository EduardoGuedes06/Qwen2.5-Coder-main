import json
import os
from abc import ABC, abstractmethod
from typing import List
from warnings import warn
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from vllm import LLM, SamplingParams

EOS = [
    "<|endoftext|>",
    "<|endofmask|>",
    "</s>",
    "\nif __name__",
    "\ndef main(",
    "\nprint(",
]


def extra_eos_for_direct_completion(dataset) -> List[str]:
    if dataset.lower() == "bigcodebench":
        return ["\ndef ", "\nclass ", "\nimport ", "\nfrom ", "\nassert "]
    raise ValueError(f"Unknown dataset: {dataset}")


# some random words which serves as the splitter
_MAGIC_SPLITTER_ = "-[[]]-this-is-really-our-highest-priority-[[]]-"


def make_chat_prompt(
    prompt: str,
    tokenizer: AutoTokenizer,
    chat_mode,
) -> str:
    if not chat_mode:  # complete tasks
        return prompt

    prompt = f"""\
Please provide a self-contained Python script that solves the following problem in a markdown code block:
```
{prompt.strip()}
```
"""
    response = f"""\
Below is a Python script with a self-contained function that solves the problem and passes corresponding tests:
```python
{_MAGIC_SPLITTER_}
```
"""
    prompt = tokenizer.apply_chat_template(
        [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response},
        ],
        tokenize=False,
    ).split(
        _MAGIC_SPLITTER_
    )[0]
    return prompt


class DecoderBase(ABC):

    def __init__(
        self,
        name: str,
        batch_size: int = 1,
        temperature: float = 0.8,
        max_new_tokens: int = 1280,
        dtype: str = "bfloat16",  # default
        trust_remote_code: bool = True,
        tokenizer_name: str = None,
        tokenizer_legacy: bool = False,
        chat_mode: bool = False,
    ) -> None:
        print("Initializing a decoder model: {} ...".format(name))
        self.name = name
        self.batch_size = batch_size
        self.temperature = temperature
        self.eos = EOS
        self.skip_special_tokens = False
        self.max_new_tokens = max_new_tokens
        self.dtype = dtype
        self.trust_remote_code = trust_remote_code
        self.tokenizer_name = tokenizer_name
        self.tokenizer_legacy = tokenizer_legacy
        self.chat_mode = chat_mode

    @abstractmethod
    def codegen(self, prompt: str, do_sample: bool = True, num_samples: int = 200) -> List[str]:
        pass

    @abstractmethod
    def is_direct_completion(self) -> bool:
        pass

    def __repr__(self) -> str:
        return self.name

    def __str__(self) -> str:
        return self.name


class VllmDecoder(DecoderBase):

    def __init__(self, name: str, dataset: str, tp: int, **kwargs) -> None:
        super().__init__(name, **kwargs)

        kwargs = {
            "tensor_parallel_size": int(os.getenv("VLLM_N_GPUS", tp)),
            "dtype": self.dtype,
            "trust_remote_code": self.trust_remote_code,
            "gpu_memory_utilization": 0.95,
            "enforce_eager": True,
            "distributed_executor_backend": "ray",
        }
        if self.tokenizer_name is None:
            self.tokenizer_name = self.name

        self.tokenizer = AutoTokenizer.from_pretrained(self.tokenizer_name, **kwargs, legacy=self.tokenizer_legacy)
        if not self.chat_mode:
            self.eos += extra_eos_for_direct_completion(dataset)
        self.llm = LLM(model=name, max_model_len=2048, **kwargs)
        self.llm.set_tokenizer(tokenizer=self.tokenizer)

    def is_direct_completion(self) -> bool:
        return not self.chat_mode

    def codegen(self, prompts: List[str], do_sample: bool = True, num_samples: int = 200) -> List[str]:
        if do_sample:
            assert self.temperature > 0, "Temperature must be greater than 0!"

        vllm_outputs = self.llm.generate(
            prompts,
            SamplingParams(
                temperature=self.temperature,
                max_tokens=self.max_new_tokens,
                top_p=0.95 if do_sample else 1.0,
                stop=self.eos,
            ),
            use_tqdm=True,
        )

        gen_strs = [x.outputs[0].text.replace("\t", "    ") for x in vllm_outputs]
        return gen_strs


class GeneralVllmDecoder(VllmDecoder):

    def __init__(self, name: str, **kwargs) -> None:
        super().__init__(name, **kwargs)
        self.eos += ["\n```\n", "```"]
        print(f"EOS strings: {self.eos}")

    def codegen(self, prompts: List[str], do_sample: bool = True, num_samples: int = 200) -> List[str]:
        chat_prompts = [
            make_chat_prompt(
                prompt,
                self.tokenizer,
                self.chat_mode,
            )
            for prompt in prompts
        ]
        return VllmDecoder.codegen(self, chat_prompts, do_sample, num_samples)


class HfTorchDecoder(DecoderBase):

    def __init__(self, name: str, dataset: str, **kwargs):
        super().__init__(name=name, **kwargs)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        kwargs = {}
        kwargs["device_map"] = "auto"
        kwargs["trust_remote_code"] = self.trust_remote_code
        # string to torch dtype
        kwargs["torch_dtype"] = getattr(torch, self.dtype)
        self.skip_special_tokens = True

        print(f"{kwargs = }", self.tokenizer_name)
        if self.tokenizer_name is None:
            self.tokenizer_name = self.name

        self.tokenizer = AutoTokenizer.from_pretrained(self.tokenizer_name, **kwargs, legacy=self.tokenizer_legacy)

        if not self.chat_mode:
            self.eos += extra_eos_for_direct_completion(dataset)

        self.model = AutoModelForCausalLM.from_pretrained(name, **kwargs)
        self.model = self.model.to(self.device)

    def is_direct_completion(self) -> bool:
        return not self.chat_mode

    @torch.inference_mode()
    def codegen(self, prompt: str, do_sample: bool = True, num_samples: int = 200) -> List[str]:
        if self.temperature == 0:
            assert not do_sample
            assert num_samples == 1

        input_tokens = self.tokenizer.encode(prompt, return_tensors="pt").to(self.device)
        kwargs = {}
        if do_sample:
            kwargs["top_p"] = 0.95
            kwargs["temperature"] = self.temperature

        outputs = self.model.generate(
            input_tokens,
            max_new_tokens=self.max_new_tokens,
            do_sample=do_sample,
            num_return_sequences=min(self.batch_size, num_samples),
            pad_token_id=self.tokenizer.eos_token_id,
            stop_strings=self.eos,
            tokenizer=self.tokenizer,
            **kwargs,
        )

        gen_strs = self.tokenizer.batch_decode(
            outputs[:, input_tokens.size(-1) :],
            skip_special_tokens=self.skip_special_tokens,
        )
        outputs = []
        # removes eos tokens.
        for output in gen_strs:
            min_index = 10000
            for eos in self.eos:
                if eos in output:
                    min_index = min(min_index, output.index(eos))
            outputs.append(output[:min_index].replace("\t", "    "))
        return outputs


class GenenralHfTorchDecoder(HfTorchDecoder):

    def __init__(self, name: str, **kwargs):
        super().__init__(name=name, **kwargs)
        self.eos += ["\n```\n", "```"]
        print(f"EOS strings: {self.eos}")
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.tokenizer_name if self.tokenizer_name else self.name, **kwargs, legacy=self.tokenizer_legacy
        )

    def codegen(self, prompt: str, do_sample: bool = True, num_samples: int = 200) -> List[str]:
        prompt = make_chat_prompt(prompt, self.tokenizer, self.chat_mode)
        return HfTorchDecoder.codegen(self, prompt, do_sample, num_samples)


def make_model(
    model: str,
    backend: str,
    dataset: str = "bigcodebench",
    batch_size: int = 1,
    temperature: float = 0.0,
    tp=1,
    base_url=None,
    trust_remote_code=True,
    tokenizer_name=None,
    tokenizer_legacy=True,
    chat_mode=False,
):
    print(f"{chat_mode = }")
    if backend == "vllm":
        return GeneralVllmDecoder(
            name=model,
            batch_size=batch_size,
            temperature=temperature,
            dataset=dataset,
            tp=tp,
            trust_remote_code=trust_remote_code,
            tokenizer_name=tokenizer_name,
            tokenizer_legacy=tokenizer_legacy,
            chat_mode=chat_mode,
        )
    elif backend == "hf":
        return GenenralHfTorchDecoder(
            name=model,
            batch_size=batch_size,
            temperature=temperature,
            dataset=dataset,
            trust_remote_code=trust_remote_code,
            tokenizer_name=tokenizer_name,
            tokenizer_legacy=tokenizer_legacy,
            chat_mode=chat_mode,
        )
