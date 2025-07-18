"""
The `MapOperation` and `ParallelMapOperation` classes are subclasses of `BaseOperation` that perform mapping operations on input data. They use LLM-based processing to transform input items into output items based on specified prompts and schemas, and can also perform key dropping operations.
"""

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from jinja2 import Template
from litellm.utils import ModelResponse
from pydantic import Field, field_validator
from tqdm import tqdm

from docetl.base_schemas import Tool, ToolFunction
from docetl.operations.base import BaseOperation
from docetl.operations.utils import RichLoopBar, strict_render
from docetl.operations.utils.api import OutputMode


class MapOperation(BaseOperation):
    class schema(BaseOperation.schema):
        type: str = "map"
        output: Optional[Dict[str, Any]] = None
        prompt: Optional[str] = None
        model: Optional[str] = None
        optimize: Optional[bool] = None
        recursively_optimize: Optional[bool] = None
        sample_size: Optional[int] = None
        tools: Optional[List[Dict[str, Any]]] = (
            None  # FIXME: Why isn't this using the Tool data class so validation works automatically?
        )
        validation_rules: Optional[List[str]] = Field(None, alias="validate")
        num_retries_on_validate_failure: Optional[int] = None
        gleaning: Optional[Dict[str, Any]] = None
        drop_keys: Optional[List[str]] = None
        timeout: Optional[int] = None
        enable_observability: bool = False
        batch_size: Optional[int] = None
        clustering_method: Optional[str] = None
        batch_prompt: Optional[str] = None
        litellm_completion_kwargs: Dict[str, Any] = {}
        pdf_url_key: Optional[str] = None
        flush_partial_result: bool = False
        # Calibration parameters
        calibrate: bool = False
        num_calibration_docs: int = 10

        @field_validator("drop_keys")
        def validate_drop_keys(cls, v):
            if isinstance(v, str):
                return [v]
            return v

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.max_batch_size: int = self.config.get(
            "max_batch_size", kwargs.get("max_batch_size", None)
        )
        self.clustering_method = "random"

    def _generate_calibration_context(self, input_data: List[Dict]) -> str:
        """
        Generate calibration context by running the operation on a sample of documents
        and using an LLM to suggest prompt improvements for consistency.

        Returns:
            str: Additional context to add to the original prompt
        """
        import random

        # Set seed for reproducibility
        random.seed(42)

        # Sample documents for calibration
        num_calibration_docs = min(
            self.config.get("num_calibration_docs", 10), len(input_data)
        )
        if num_calibration_docs == len(input_data):
            calibration_sample = input_data
        else:
            calibration_sample = random.sample(input_data, num_calibration_docs)

        self.console.log(
            f"[bold blue]Running calibration on {num_calibration_docs} documents...[/bold blue]"
        )

        # Temporarily disable calibration to avoid infinite recursion
        original_calibrate = self.config.get("calibrate", False)
        self.config["calibrate"] = False

        try:
            # Run the map operation on the calibration sample
            calibration_results, _ = self.execute(calibration_sample)

            # Prepare the calibration analysis prompt
            calibration_prompt = f"""
The following prompt was applied to sample documents to generate these input-output pairs:

"{self.config["prompt"]}"

Sample inputs and their outputs:
"""

            for i, (input_doc, output_doc) in enumerate(
                zip(calibration_sample, calibration_results)
            ):
                calibration_prompt += f"\n--- Example {i+1} ---\n"
                calibration_prompt += f"Input: {input_doc}\n"
                calibration_prompt += f"Output: {output_doc}\n"

            calibration_prompt += """
Based on these examples, provide reference anchors that will be appended to the prompt to help maintain consistency when processing all documents.

DO NOT provide generic advice. Instead, use specific examples from above as calibration points.
Note that the outputs might be incorrect, because the user's prompt was not calibrated or rich in the first place.
You can ignore the outputs if they are incorrect, and focus on the diversity of the inputs.

Format as concrete reference points:
- "For reference, consider '[specific input text]' → [output] as a baseline for [category/level]"
- "Documents similar to '[specific input text]' should be classified as [output]"

Reference anchors:"""

            # Call LLM to get calibration suggestions
            messages = [{"role": "user", "content": calibration_prompt}]
            completion_kwargs = self.config.get("litellm_completion_kwargs", {})
            completion_kwargs["temperature"] = 0.0

            llm_result = self.runner.api.call_llm(
                self.config.get("model", self.default_model),
                "calibration",
                messages,
                {"calibration_context": "string"},
                timeout_seconds=self.config.get("timeout", 120),
                max_retries_per_timeout=self.config.get("max_retries_per_timeout", 2),
                bypass_cache=self.config.get("bypass_cache", self.bypass_cache),
                litellm_completion_kwargs=completion_kwargs,
                op_config=self.config,
            )

            # Parse the response
            if hasattr(llm_result, "response"):
                calibration_context = self.runner.api.parse_llm_response(
                    llm_result.response,
                    schema={"calibration_context": "string"},
                    manually_fix_errors=self.manually_fix_errors,
                )[0].get("calibration_context", "")
            else:
                calibration_context = ""

            return calibration_context

        finally:
            # Restore original calibration setting
            self.config["calibrate"] = original_calibrate

    def syntax_check(self) -> None:
        """
            Checks the configuration of the MapOperation for required keys and valid structure.

        Raises:
            ValueError: If required keys are missing or invalid in the configuration.
            TypeError: If configuration values have incorrect types.
        """
        config = self.schema(**self.config)

        if config.drop_keys:
            if any(not isinstance(key, str) for key in config.drop_keys):
                raise TypeError("All items in 'drop_keys' must be strings")
        elif not (config.prompt and config.output):
            raise ValueError(
                "If 'drop_keys' is not specified, both 'prompt' and 'output' must be present in the configuration"
            )

        # Validate calibration parameters
        if config.calibrate and not isinstance(config.calibrate, bool):
            raise TypeError("'calibrate' must be a boolean")

        if config.num_calibration_docs and not isinstance(
            config.num_calibration_docs, int
        ):
            raise TypeError("'num_calibration_docs' must be an integer")

        if config.num_calibration_docs and config.num_calibration_docs <= 0:
            raise ValueError("'num_calibration_docs' must be a positive integer")

        if config.batch_prompt:
            try:
                template = Template(config.batch_prompt)
                # Test render with a minimal inputs list to validate template
                template.render(inputs=[{}])
            except Exception as e:
                raise ValueError(
                    f"Invalid Jinja2 template in 'batch_prompt' or missing required 'inputs' variable: {str(e)}"
                ) from e

        if config.prompt or config.output:
            for key in ["prompt", "output"]:
                if not getattr(config, key):
                    raise ValueError(
                        f"Missing required key '{key}' in MapOperation configuration"
                    )

            if config.output and not config.output["schema"]:
                raise ValueError("Missing 'schema' in 'output' configuration")

            if config.prompt:
                try:
                    Template(config.prompt)
                except Exception as e:
                    raise ValueError(
                        f"Invalid Jinja2 template in 'prompt': {str(e)}"
                    ) from e

            if config.model and not isinstance(config.model, str):
                raise TypeError("'model' in configuration must be a string")

            if config.tools:
                for tool in config.tools:
                    try:
                        tool_obj = Tool(**tool)
                    except Exception:
                        raise TypeError("Tool must be a dictionary")

                    if not (tool_obj.code and tool_obj.function):
                        raise ValueError(
                            "Tool is missing required 'code' or 'function' key"
                        )

                    if not isinstance(tool_obj.function, ToolFunction):
                        raise TypeError("'function' in tool must be a dictionary")

                    for key in ["name", "description", "parameters"]:
                        if not getattr(tool_obj.function, key):
                            raise ValueError(
                                f"Tool is missing required '{key}' in 'function'"
                            )

            self.gleaning_check()

    def execute(self, input_data: List[Dict]) -> Tuple[List[Dict], float]:
        """
        Executes the map operation on the provided input data.

        Args:
            input_data (List[Dict]): The input data to process.

        Returns:
            Tuple[List[Dict], float]: A tuple containing the processed results and the total cost of the operation.

        This method performs the following steps:
        1. If calibration is enabled, runs calibration to improve prompt consistency
        2. If a prompt is specified, it processes each input item using the specified prompt and LLM model
        3. Applies gleaning if configured
        4. Validates the output
        5. If drop_keys is specified, it drops the specified keys from each document
        6. Aggregates results and calculates total cost

        The method uses parallel processing to improve performance.
        """
        # Check if there's no prompt and only drop_keys
        if "prompt" not in self.config and "drop_keys" in self.config:
            # If only drop_keys is specified, simply drop the keys and return
            dropped_results = []
            for item in input_data:
                new_item = {
                    k: v for k, v in item.items() if k not in self.config["drop_keys"]
                }
                dropped_results.append(new_item)
            return dropped_results, 0.0  # Return the modified data with no cost

        # Generate calibration context if enabled
        calibration_context = ""
        if self.config.get("calibrate", False) and "prompt" in self.config:
            calibration_context = self._generate_calibration_context(input_data)
            if calibration_context:
                # Store original prompt for potential restoration
                self._original_prompt = self.config["prompt"]
                # Augment the prompt with calibration context
                self.config["prompt"] = (
                    f"{self.config['prompt']}\n\n{calibration_context}"
                )
                self.console.log(
                    f"[bold green]New map ({self.config['name']}) prompt augmented with context on how to improve consistency:[/bold green] {self.config['prompt']}"
                )
            else:
                self.console.log(
                    f"[bold yellow]Extra context on how to improve consistency failed to generate for map ({self.config['name']}); continuing with prompt as is.[/bold yellow]"
                )

        if self.status:
            self.status.stop()

        def _process_map_item(
            item: Dict, initial_result: Optional[Dict] = None
        ) -> Tuple[Optional[List[Dict]], float]:

            prompt = strict_render(self.config["prompt"], {"input": item})
            messages = [{"role": "user", "content": prompt}]
            if self.config.get("pdf_url_key", None):
                # Append the pdf to the prompt
                try:
                    pdf_url = item[self.config["pdf_url_key"]]
                except KeyError:
                    raise ValueError(
                        f"PDF URL key '{self.config['pdf_url_key']}' not found in input data"
                    )

                # Download content
                if pdf_url.startswith("http"):
                    file_data = requests.get(pdf_url).content
                else:
                    with open(pdf_url, "rb") as f:
                        file_data = f.read()
                encoded_file = base64.b64encode(file_data).decode("utf-8")
                base64_url = f"data:application/pdf;base64,{encoded_file}"

                messages[0]["content"] = [
                    {"type": "image_url", "image_url": {"url": base64_url}},
                    {"type": "text", "text": prompt},
                ]

            def validation_fn(response: Union[Dict[str, Any], ModelResponse]):
                structured_mode = (
                    self.config.get("output", {}).get("mode")
                    == OutputMode.STRUCTURED_OUTPUT.value
                )
                output = (
                    self.runner.api.parse_llm_response(
                        response,
                        schema=self.config["output"]["schema"],
                        tools=self.config.get("tools", None),
                        manually_fix_errors=self.manually_fix_errors,
                        use_structured_output=structured_mode,
                    )[0]
                    if isinstance(response, ModelResponse)
                    else response
                )
                # Check that the output has all the keys in the schema
                for key in self.config["output"]["schema"]:
                    if key not in output:
                        return output, False

                for key, value in item.items():
                    if key not in self.config["output"]["schema"]:
                        output[key] = value
                if self.runner.api.validate_output(self.config, output, self.console):
                    return output, True
                return output, False

            if self.runner.is_cancelled:
                raise asyncio.CancelledError("Operation was cancelled")
            llm_result = self.runner.api.call_llm(
                self.config.get("model", self.default_model),
                "map",
                messages,
                self.config["output"]["schema"],
                tools=self.config.get("tools", None),
                scratchpad=None,
                timeout_seconds=self.config.get("timeout", 120),
                max_retries_per_timeout=self.config.get("max_retries_per_timeout", 2),
                validation_config=(
                    {
                        "num_retries": self.num_retries_on_validate_failure,
                        "val_rule": self.config.get("validate", []),
                        "validation_fn": validation_fn,
                    }
                    if self.config.get("validate", None)
                    else None
                ),
                gleaning_config=self.config.get("gleaning", None),
                verbose=self.config.get("verbose", False),
                bypass_cache=self.config.get("bypass_cache", self.bypass_cache),
                initial_result=initial_result,
                litellm_completion_kwargs=self.config.get(
                    "litellm_completion_kwargs", {}
                ),
                op_config=self.config,
            )

            if llm_result.validated:
                # Parse the response
                if isinstance(llm_result.response, ModelResponse):
                    structured_mode = (
                        self.config.get("output", {}).get("mode")
                        == OutputMode.STRUCTURED_OUTPUT.value
                    )
                    outputs = self.runner.api.parse_llm_response(
                        llm_result.response,
                        schema=self.config["output"]["schema"],
                        tools=self.config.get("tools", None),
                        manually_fix_errors=self.manually_fix_errors,
                        use_structured_output=structured_mode,
                    )
                else:
                    outputs = [llm_result.response]

                # Augment the output with the original item
                outputs = [{**item, **output} for output in outputs]
                if self.config.get("enable_observability", False):
                    for output in outputs:
                        output[f"_observability_{self.config['name']}"] = {
                            "prompt": prompt
                        }
                return outputs, llm_result.total_cost

            return None, llm_result.total_cost

        # If there's a batch prompt, let's use that
        def _process_map_batch(items: List[Dict]) -> Tuple[List[Dict], float]:
            total_cost = 0
            if len(items) > 1 and self.config.get("batch_prompt", None):
                # Raise error if pdf_url_key is set
                if self.config.get("pdf_url_key", None):
                    raise ValueError("Batch prompts do not support PDF URLs")

                batch_prompt = strict_render(
                    self.config["batch_prompt"], {"inputs": items}
                )

                # Issue the batch call
                llm_result = self.runner.api.call_llm_batch(
                    self.config.get("model", self.default_model),
                    "batch map",
                    [{"role": "user", "content": batch_prompt}],
                    self.config["output"]["schema"],
                    verbose=self.config.get("verbose", False),
                    timeout_seconds=self.config.get("timeout", 120),
                    max_retries_per_timeout=self.config.get(
                        "max_retries_per_timeout", 2
                    ),
                    bypass_cache=self.config.get("bypass_cache", self.bypass_cache),
                    litellm_completion_kwargs=self.config.get(
                        "litellm_completion_kwargs", {}
                    ),
                )
                total_cost += llm_result.total_cost

                # Parse the LLM response
                structured_mode = (
                    self.config.get("output", {}).get("mode")
                    == OutputMode.STRUCTURED_OUTPUT.value
                )
                parsed_output = self.runner.api.parse_llm_response(
                    llm_result.response,
                    self.config["output"]["schema"],
                    use_structured_output=structured_mode,
                )[0].get("results", [])
                items_and_outputs = [
                    (item, parsed_output[idx] if idx < len(parsed_output) else None)
                    for idx, item in enumerate(items)
                ]
            else:
                items_and_outputs = [(item, None) for item in items]

            # Run _process_map_item for each item
            all_results = []
            if len(items_and_outputs) > 1:
                with ThreadPoolExecutor(max_workers=self.max_batch_size) as executor:
                    futures = [
                        executor.submit(
                            _process_map_item,
                            items_and_outputs[i][0],
                            items_and_outputs[i][1],
                        )
                        for i in range(len(items_and_outputs))
                    ]
                    for i in range(len(futures)):
                        try:
                            results, item_cost = futures[i].result()
                            if results is not None:
                                all_results.extend(results)
                            total_cost += item_cost
                        except Exception as e:
                            if self.config.get("skip_on_error", False):
                                self.console.log(
                                    f"[bold red]Error in map operation {self.config['name']}, skipping item:[/bold red] {e}"
                                )
                                continue
                            else:
                                raise e
            else:
                try:
                    results, item_cost = _process_map_item(
                        items_and_outputs[0][0], items_and_outputs[0][1]
                    )
                    if results is not None:
                        all_results.extend(results)
                    total_cost += item_cost
                except Exception as e:
                    if self.config.get("skip_on_error", False):
                        self.console.log(
                            f"[bold red]Error in map operation {self.config['name']}, skipping item:[/bold red] {e}"
                        )
                    else:
                        raise e

            return all_results, total_cost

        with ThreadPoolExecutor(max_workers=self.max_batch_size) as executor:
            batch_size = self.max_batch_size if self.max_batch_size is not None else 1
            futures = []
            for i in range(0, len(input_data), batch_size):
                batch = input_data[i : i + batch_size]
                futures.append(executor.submit(_process_map_batch, batch))
            results = []
            total_cost = 0
            pbar = RichLoopBar(
                range(len(futures)),
                desc=f"Processing {self.config['name']} (map) on all documents",
                console=self.console,
            )
            for batch_index in pbar:
                result_list, item_cost = futures[batch_index].result()
                if result_list:
                    if "drop_keys" in self.config:
                        result_list = [
                            {
                                k: v
                                for k, v in result.items()
                                if k not in self.config["drop_keys"]
                            }
                            for result in result_list
                        ]
                    results.extend(result_list)
                    # --- BEGIN: Flush partial checkpoint ---
                    if self.config.get("flush_partial_results", False):
                        op_name = self.config["name"]
                        self.runner._flush_partial_results(
                            op_name, batch_index, result_list
                        )
                    # --- END: Flush partial checkpoint ---
                total_cost += item_cost

        if self.status:
            self.status.start()

        return results, total_cost


class ParallelMapOperation(BaseOperation):
    class schema(BaseOperation.schema):
        type: str = "parallel_map"
        prompts: List[Dict[str, Any]]
        output: Dict[str, Any]
        enable_observability: bool = False
        pdf_url_key: Optional[str] = None

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

    def syntax_check(self) -> None:
        """
        Checks the configuration of the ParallelMapOperation for required keys and valid structure.

        Raises:
            ValueError: If required keys are missing or if the configuration structure is invalid.
            TypeError: If the configuration values have incorrect types.
        """
        if "drop_keys" in self.config:
            if not isinstance(self.config["drop_keys"], list):
                raise TypeError(
                    "'drop_keys' in configuration must be a list of strings"
                )
            for key in self.config["drop_keys"]:
                if not isinstance(key, str):
                    raise TypeError("All items in 'drop_keys' must be strings")
        elif "prompts" not in self.config:
            raise ValueError(
                "If 'drop_keys' is not specified, 'prompts' must be present in the configuration"
            )

        if "prompts" in self.config:
            if not isinstance(self.config["prompts"], list):
                raise ValueError(
                    "ParallelMapOperation requires a 'prompts' list in the configuration"
                )

            if not self.config["prompts"]:
                raise ValueError("The 'prompts' list cannot be empty")

            for i, prompt_config in enumerate(self.config["prompts"]):
                if not isinstance(prompt_config, dict):
                    raise TypeError(f"Prompt configuration {i} must be a dictionary")

                required_keys = ["prompt", "output_keys"]
                for key in required_keys:
                    if key not in prompt_config:
                        raise ValueError(
                            f"Missing required key '{key}' in prompt configuration {i}"
                        )
                if not isinstance(prompt_config["prompt"], str):
                    raise TypeError(
                        f"'prompt' in prompt configuration {i} must be a string"
                    )

                if not isinstance(prompt_config["output_keys"], list):
                    raise TypeError(
                        f"'output_keys' in prompt configuration {i} must be a list"
                    )

                if not prompt_config["output_keys"]:
                    raise ValueError(
                        f"'output_keys' list in prompt configuration {i} cannot be empty"
                    )

                # Check if the prompt is a valid Jinja2 template
                try:
                    Template(prompt_config["prompt"])
                except Exception as e:
                    raise ValueError(
                        f"Invalid Jinja2 template in prompt configuration {i}: {str(e)}"
                    ) from e

                # Check if the model is specified (optional)
                if "model" in prompt_config and not isinstance(
                    prompt_config["model"], str
                ):
                    raise TypeError(
                        f"'model' in prompt configuration {i} must be a string"
                    )

            # Check if all output schema keys are covered by the prompts
            output_schema = self.config["output"]["schema"]
            output_keys_covered = set()
            for prompt_config in self.config["prompts"]:
                output_keys_covered.update(prompt_config["output_keys"])

            missing_keys = set(output_schema.keys()) - output_keys_covered
            if missing_keys:
                raise ValueError(
                    f"The following output schema keys are not covered by any prompt: {missing_keys}"
                )

    def execute(self, input_data: List[Dict]) -> Tuple[List[Dict], float]:
        """
        Executes the parallel map operation on the provided input data.

        Args:
            input_data (List[Dict]): The input data to process.

        Returns:
            Tuple[List[Dict], float]: A tuple containing the processed results and the total cost of the operation.

        This method performs the following steps:
        1. If prompts are specified, it processes each input item using multiple prompts in parallel
        2. Aggregates results from different prompts for each input item
        3. Validates the combined output for each item
        4. If drop_keys is specified, it drops the specified keys from each document
        5. Calculates total cost of the operation
        """
        results = {}
        total_cost = 0
        output_schema = self.config.get("output", {}).get("schema", {})

        # Check if there's no prompt and only drop_keys
        if "prompts" not in self.config and "drop_keys" in self.config:
            # If only drop_keys is specified, simply drop the keys and return
            dropped_results = []
            for item in input_data:
                new_item = {
                    k: v for k, v in item.items() if k not in self.config["drop_keys"]
                }
                dropped_results.append(new_item)
            return dropped_results, 0.0  # Return the modified data with no cost

        if self.status:
            self.status.stop()

        def process_prompt(item, prompt_config):
            prompt = strict_render(prompt_config["prompt"], {"input": item})
            messages = [{"role": "user", "content": prompt}]
            if self.config.get("pdf_url_key", None):
                try:
                    pdf_url = item[self.config["pdf_url_key"]]
                except KeyError:
                    raise ValueError(
                        f"PDF URL key '{self.config['pdf_url_key']}' not found in input data"
                    )
                # Download content
                if pdf_url.startswith("http"):
                    file_data = requests.get(pdf_url).content
                else:
                    with open(pdf_url, "rb") as f:
                        file_data = f.read()
                encoded_file = base64.b64encode(file_data).decode("utf-8")
                base64_url = f"data:application/pdf;base64,{encoded_file}"

                messages[0]["content"] = [
                    {"type": "image_url", "image_url": {"url": base64_url}},
                    {"type": "text", "text": prompt},
                ]

            local_output_schema = {
                key: output_schema.get(key, "string")
                for key in prompt_config["output_keys"]
            }
            model = prompt_config.get("model", self.default_model)
            if not model:
                model = self.default_model

            # Start of Selection
            # If there are tools, we need to pass in the tools
            response = self.runner.api.call_llm(
                model,
                "parallel_map",
                messages,
                local_output_schema,
                tools=prompt_config.get("tools", None),
                timeout_seconds=self.config.get("timeout", 120),
                max_retries_per_timeout=self.config.get("max_retries_per_timeout", 2),
                gleaning_config=prompt_config.get("gleaning", None),
                bypass_cache=self.config.get("bypass_cache", self.bypass_cache),
                litellm_completion_kwargs=self.config.get(
                    "litellm_completion_kwargs", {}
                ),
                op_config=self.config,
            )
            structured_mode = (
                self.config.get("output", {}).get("mode")
                == OutputMode.STRUCTURED_OUTPUT.value
            )
            output = self.runner.api.parse_llm_response(
                response.response,
                schema=local_output_schema,
                tools=prompt_config.get("tools", None),
                manually_fix_errors=self.manually_fix_errors,
                use_structured_output=structured_mode,
            )[0]
            return output, prompt, response.total_cost

        with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            if "prompts" in self.config:
                # Create all futures at once
                all_futures = [
                    executor.submit(process_prompt, item, prompt_config)
                    for item in input_data
                    for prompt_config in self.config["prompts"]
                ]

                # Process results in order
                for i in tqdm(
                    range(len(all_futures)),
                    desc="Processing parallel map items",
                ):
                    future = all_futures[i]
                    output, prompt, cost = future.result()
                    total_cost += cost

                    # Determine which item this future corresponds to
                    item_index = i // len(self.config["prompts"])
                    prompt_index = i % len(self.config["prompts"])

                    # Initialize or update the item_result
                    if prompt_index == 0:
                        item_result = input_data[item_index].copy()
                        results[item_index] = item_result

                    # Fetch the item_result
                    item_result = results[item_index]

                    if self.config.get("enable_observability", False):
                        if f"_observability_{self.config['name']}" not in item_result:
                            item_result[f"_observability_{self.config['name']}"] = {}
                        item_result[f"_observability_{self.config['name']}"].update(
                            {f"prompt_{prompt_index}": prompt}
                        )

                    # Update the item_result with the output
                    item_result.update(output)

            else:
                results = {i: item.copy() for i, item in enumerate(input_data)}

        # Apply drop_keys if specified
        if "drop_keys" in self.config:
            drop_keys = self.config["drop_keys"]
            for item in results.values():
                for key in drop_keys:
                    item.pop(key, None)

        if self.status:
            self.status.start()

        # Return the results in order
        return [results[i] for i in range(len(input_data)) if i in results], total_cost
