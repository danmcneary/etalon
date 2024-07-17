import io
import json
import os
import time
from typing import Tuple

import boto3
import ray

from metron.core.llm_clients.base_llm_client import BaseLLMClient
from metron.core.request_config import RequestConfig
from metron.logger import init_logger
from metron.metrics.request_metrics import RequestMetrics

logger = init_logger(__name__)


class SageMakerClient(BaseLLMClient):
    """Client for OpenAI Chat Completions API."""

    async def send_llm_request(
        self, request_config: RequestConfig
    ) -> Tuple[RequestMetrics, str]:
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            raise ValueError("AWS_ACCESS_KEY_ID must be set.")
        if not os.environ.get("AWS_SECRET_ACCESS_KEY"):
            raise ValueError("AWS_SECRET_ACCESS_KEY must be set.")
        if not os.environ.get("AWS_REGION_NAME"):
            raise ValueError("AWS_REGION_NAME must be set.")

        prompt = request_config.prompt
        prompt, prompt_len = prompt

        message = [
            {"role": "system", "content": ""},
            {"role": "user", "content": prompt},
        ]
        model = request_config.model
        sm_runtime = boto3.client(
            "sagemaker-runtime", region_name=os.environ.get("AWS_REGION_NAME")
        )

        sampling_params = request_config.sampling_params

        if "max_tokens" in sampling_params:
            sampling_params["max_new_tokens"] = sampling_params["max_tokens"]
            del sampling_params["max_tokens"]

        message = {
            "inputs": [
                [
                    {"role": "system", "content": ""},
                    {"role": "user", "content": prompt},
                ]
            ],
            "parameters": {
                **request_config.sampling_params,
            },
        }

        inter_token_times = []
        tokens_received = 0
        error_msg = None
        error_response_code = None
        generated_text = ""

        most_recent_received_token_time = time.monotonic()

        try:
            response = sm_runtime.invoke_endpoint_with_response_stream(
                EndpointName=model,
                ContentType="application/json",
                Body=json.dumps(message),
                CustomAttributes="accept_eula=true",
            )

            event_stream = response["Body"]
            json_byte = b""
            for line, _, _ in LineIterator(event_stream):
                json_byte += line
                inter_token_times.append(
                    time.monotonic() - most_recent_received_token_time
                )
                most_recent_received_token_time = time.monotonic()
            resp = json.loads(json_byte)
            generated_text = resp[0]["generation"]["content"]
            tokens_received = self.get_token_length(generated_text)
        except Exception as e:
            logger.error(f"Warning Or Error: ({error_response_code}) {e}")
            error_msg = str(e)
            error_response_code = 500

        metrics = RequestMetrics(
            inter_token_times=inter_token_times,
            num_prompt_tokens=prompt_len,
            num_output_tokens=tokens_received,
            error_code=error_response_code,
            error_msg=error_msg,
        )

        return metrics, generated_text


class LineIterator:
    """
    A helper class for parsing the byte stream input.
    Reference: https://aws.amazon.com/blogs/machine-learning/elevating-the-generative-ai-experience-introducing-streaming-support-in-amazon-sagemaker-hosting/
    """

    def __init__(self, stream):
        self.byte_iterator = iter(stream)
        self.buffer = io.BytesIO()
        self.read_pos = 0
        self.ttft = 0

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            self.buffer.seek(self.read_pos)
            line = self.buffer.readline()
            if line and line[-1] == ord("\n"):
                if self.ttft == 0:
                    self.ttft = time.monotonic()
                self.read_pos += len(line)
                return line[:-1], self.ttft, time.monotonic()
            # kyle: dealing with last ']' for chat output
            if line and self.read_pos == self.buffer.getbuffer().nbytes - 1:
                self.read_pos += 1
                return line, self.ttft, time.monotonic()
            try:
                chunk = next(self.byte_iterator)
            except StopIteration:
                if self.read_pos < self.buffer.getbuffer().nbytes:
                    continue
                raise
            if "PayloadPart" not in chunk:
                logger.error(f"Unknown event type: {chunk}")
                continue
            self.buffer.seek(0, io.SEEK_END)
            self.buffer.write(chunk["PayloadPart"]["Bytes"])
