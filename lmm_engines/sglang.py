import os
import time
import torch
import random
import openai
import importlib.util
from pathlib import Path
from typing import List

from sglang import function, system, user, assistant, gen, set_default_backend, RuntimeEndpoint
from .utils import SubprocessMonitor, ChatTokenizer
worker_initiated = False
sglang_workers = {}
def launch_sglang_worker(
    model_name: str,
    num_gpus: int=None,
    gpu_ids: List[int]=None,
    dtype: str="auto",
    port: int=34200,
    host: str="127.0.0.1",
) -> str:
    """
    Launch a model worker and return the address
    Args:
        model_name: the model name to launch
    Returns:
        the address of the launched model
    """
    # python -m sglang.launch_server --model-path meta-llama/Meta-Llama-3-8B-Instruct --port 30000
    worker_addr = f"http://{host}:{port}"
    log_file = Path(os.path.abspath(__file__)).parent / "logs" / f"{model_name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    if gpu_ids:
        num_gpus = len(gpu_ids)
    else:
        if not num_gpus:
            num_gpus = torch.cuda.device_count()
            print(f"Warning: num_gpus or gpu_ids not provided, using {num_gpus} GPUs")
        gpu_ids = list(range(num_gpus))
    env = os.environ.copy()
    # Set the CUDA_VISIBLE_DEVICES environment variable
    env["CUDA_VISIBLE_DEVICES"] = ",".join([str(gpu_id) for gpu_id in gpu_ids])
    
    # check flashinfer
    flashinfer = importlib.util.find_spec("flashinfer")
    if flashinfer is None:
        print("flashinfer not found, disable flashinfer for sglang")
        flashinfer_args = ["--disable-flashinfer"]
    else:
        print("flashinfer found, enable flashinfer for sglang")
        flashinfer_args = []
    additonal_ports = [port+i for i in range(1, 9)]
    proc = SubprocessMonitor([
        "python3", "-m", "sglang.launch_server",
        "--model-path", model_name,
        "--host", host,
        "--port", str(port),
        "--dtype", dtype,
        # "--api-key", "sglang",
        "--log-level", "warning",
        "--tp-size",  str(num_gpus) if num_gpus is not None else "1",
        "--additional-ports"] + [str(port) for port in additonal_ports
    ] + flashinfer_args ,env=env)
    print(f"Launching SGLang model {model_name} with CUDA_VISIBLE_DEVICES={env['CUDA_VISIBLE_DEVICES']}")
    sglang_workers[worker_addr] = proc
    return worker_addr, proc

@function
def multi_turn_question(s, messages, system_message=None):
    if system_message:
        s += system(system_message)
    for i, message in enumerate(messages):
        if i % 2 == 0:
            s += user(message)
        else:
            s += assistant(message)
    s += assistant(gen("answer"))
    
@function
def question(s, prompt):
    s += prompt
    s += gen("answer")



chat_tokenizers = {}
def call_sglang_worker(messages, model_name, worker_addrs, conv_system_msg=None, **generate_kwargs) -> str:
    global worker_initiated
    global chat_tokenizers
    
    if model_name not in chat_tokenizers:
        chat_tokenizers[model_name] = ChatTokenizer(model_name)
    chat_tokenizer = chat_tokenizers[model_name]
    
    chat_messages = []
    if conv_system_msg:
        chat_messages.append({"role": "system", "content": conv_system_msg})
    for i, message in enumerate(messages):
        chat_messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": message})

    prompt = chat_tokenizer(chat_messages)

    worker_addr = random.choice(worker_addrs)
    
    client = openai.OpenAI(
        base_url=f"{worker_addr}/v1",
        api_key="sglang-engine-token",
    )
    
    generate_kwargs['max_tokens'] = generate_kwargs['max_tokens'] or 4092 # for sglang, max_tokens is required and must > 0
    while True:
        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=chat_messages,
                **generate_kwargs,
            )
            break
        except openai.APIConnectionError as e:
            if not worker_initiated:
                time.sleep(5)
                continue
            print(f"API connection error: {e}")
            time.sleep(5)
            continue
    
    return completion.choices[0].message.content