#!/usr/bin/env python3

import argparse
import base64
from functools import cache
import hashlib
import json
import logging
import os
import re
import sys
import urllib.request

from langchain.agents import create_agent
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

from include_analysis import IncludeAnalysisOutput, ParseError, load_include_analysis
from typing import Dict

# TODO - mojo::Remote<T> usage counts as full type usage since it requires the full definition of T
PROMPT_TEMPLATE = """
Carefully analyze the includer file and determine if the include directive for "{target_file}" (target header) in the source file "{source_file}" can be replaced with forward declarations.
Think carefully step-by-step about any usage of named symbols in the target header, and do NOT think in hypotheticals, only what is present in the provided code.
Do not say "if" - only analyze the provided code.

Assume the rest of the code base follows Include What You Use (IWYU) principles and is including any headers it directly uses. Forward declarations do NOT violate IWYU
as long as the includer file does not directly use any symbols from the target header that require a full definition.

An include directive can be replaced with forward declarations if:
* The includer file only uses pointers, references, or return-by-values of types declared in the target header.
* The includer file does not need to know the size or layout of any types declared in the target header.
* The includer file does not use any functions, templates, or macros declared in the target header.
* The includer file does not use any constants or enum values declared in the target header.
* The includer file does not use any typedefs or using declarations declared in the target header.
* The includer file does not use any classes declared in the target header as a base class.
* The includer file does not use any types declared in the target header as a member variable type, unless it is a pointer, reference, raw_ptr, or smart pointer.

Things you CAN NOT forward declare:
* Inner classes or structs declared within another class or struct.
* Nested enums or other types.
* Template specializations.
* Enum values.
* Typedefs or using-declarations.
* Macros.

You *CAN* use an incomplete type as a function return-by-value type as long as the function is only declared, not defined - a function is only defined if it has a function body.
Do NOT assert that a function is defined inline if it does not have a function body in the target header file.

REMEMBER, not all named symbols are in namespaces - if you don't see a namespace, don't wrap the named symbol in one.
REMEMBER, usage of a `using` declaration requires the full definition.
REMEMBER, check for usage in constructor member initializer lists.

For context, here is the code for both files:

# Includer File: {source_file}

```cpp
{source_file_content}
```

# Target Header: {target_file}

```cpp
{target_file_content}
```
"""

MINIMIZE_CPP_HEADER_TEMPLATE = """
Given the following chunk of C++ header file content, minimize it and return only the minimized code for
that chunk.

To minimize the header file content:
* Remove any function definitions, keeping only declarations.
* Remove any inline function definitions, keeping only declarations.
* Remove any private or protected members, keeping only public members.
* Remove const qualifiers from functions.
* Remove attributes such as `[[nodiscard]]`.
* Respect indentation, do not change it.

Things you should not do:
* Do NOT try to fix or complete incomplete code - just minimize what is given.
* Do NOT add any comments or explanations - return only the minimized code.
* Do NOT prematurely close any open namespaces or classes - wait until the next chunk closes it.
* Do NOT wrap the code in Markdown code blocks.

Here is the C++ header file content to minimize:

{content}
"""


@cache
def get_model(model_name: str = None) -> ChatOpenAI:
    if not model_name:
        model_name = "gpt-4.1"

    return ChatOpenAI(
        model=model_name,
        base_url="https://models.github.ai/inference",
        api_key=os.environ["GITHUB_TOKEN"],
        temperature=0,
    )


def save_to_cache(prefix: str, file_path: str, original_content: str, content: str):
    cache_dir = os.path.join(os.path.dirname(__file__), ".llm-cache")
    cache_key = hashlib.sha256(bytes(f"{file_path}:{original_content}", "utf-8")).hexdigest()
    cache_path = os.path.join(cache_dir, f"{prefix}-{cache_key}")

    os.makedirs(cache_dir, exist_ok=True)

    if not os.path.exists(cache_path):
        with open(cache_path, "w") as f:
            f.write(content)

    return content


def load_from_cache(prefix: str, file_path: str, original_content: str) -> str | None:
    cache_dir = os.path.join(os.path.dirname(__file__), ".llm-cache")
    cache_key = hashlib.sha256(bytes(f"{file_path}:{original_content}", "utf-8")).hexdigest()
    cache_path = os.path.join(cache_dir, f"{prefix}-{cache_key}")

    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            return f.read()

    return None


def minimize_includer_header(code: str) -> str:
    """Minimize the includer header by stripping out comments, forward declarations, etc."""

    comment_regex = re.compile(r"^\s*//[^\n]+\n+", re.MULTILINE)
    fwd_decl_regex = re.compile(r"^(?:enum class|class|struct) \S+(?: : \S+)?;\n+", re.MULTILINE)
    empty_namespace_regex = re.compile(r"^namespace \S+ {\s+}\s*(?://)?[^\n]+\n+\n", re.MULTILINE)

    # Strip out metadata comment from generated files
    metadata_comment_regex = re.compile(r"^/\* Metadata comment[^\*]+\*/\n?", re.MULTILINE)

    code = re.sub(comment_regex, "", code)
    code = re.sub(fwd_decl_regex, "", code)
    code = re.sub(empty_namespace_regex, "", code)
    code = re.sub(metadata_comment_regex, "", code)

    return code.strip()


def minimize_included_header(code: str) -> str:
    """Minimize the included header by stripping out comments, includes, forward declarations, checks, etc.

    We can be more aggressive here since we just want to keep the declarations needed for forward declaration.
    """

    comment_regex = re.compile(r"^\s*//[^\n]+\n+", re.MULTILINE)
    include_regex = re.compile(r"^#include\s+[<\"].*[\">][^\n]*(?://)?[^\n]*\n+", re.MULTILINE)
    fwd_decl_regex = re.compile(r"^(?:enum class|class|struct) \S+(?: : \S+)?;\n+", re.MULTILINE)
    check_regex = re.compile(r"^\s*D?CHECK\(.*\);\n+", re.MULTILINE)
    friend_class_regex = re.compile(r"^\s*friend class \S+;\n+", re.MULTILINE)
    empty_namespace_regex = re.compile(r"^namespace \S+ {\s+}\s*(?://)?[^\n]+\n+\n", re.MULTILINE)

    # Strip out inline definitions for functions
    function_definition_regex = re.compile(
        r"^(\s*(?:const )?\S+[\*&]? \S+\([^\)]*\)(?: const)?) {[^}]+\n?\s*}\n", re.MULTILINE
    )

    # Strip out const qualifiers
    const_qualifiers_regex = re.compile(r"^(\s+)(?:const )?([^\n]+) (?:const);\n", re.MULTILINE)

    # Strip out NOINLINE macro
    noinline_macro_regex = re.compile(r"^(\s*)NOINLINE ([^\n]+);\n", re.MULTILINE)

    # Strip out metadata comment from generated files
    metadata_comment_regex = re.compile(r"^/\* Metadata comment[^\*]+\*/\n?", re.MULTILINE)

    code = re.sub(comment_regex, "", code)
    code = re.sub(include_regex, "", code)
    code = re.sub(fwd_decl_regex, "", code)
    code = re.sub(check_regex, "", code)
    code = re.sub(empty_namespace_regex, "", code)
    code = re.sub(friend_class_regex, "", code)
    code = re.sub(function_definition_regex, r"\1;\n", code)
    code = re.sub(const_qualifiers_regex, r"\1\2;\n", code)
    code = re.sub(noinline_macro_regex, r"\1\2;\n", code)
    code = re.sub(metadata_comment_regex, "", code)

    return code.strip()


def get_file_content(revision: str, file_path: str) -> str:
    cached = load_from_cache(
        "raw-content",
        f"{file_path}:{revision}",
        "",
    )

    if not cached:
        try:
            req = urllib.request.Request(
                f"https://raw.githubusercontent.com/chromium/chromium/{revision}/{file_path}",
                headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"},
            )

            cached = save_to_cache(
                "raw-content",
                f"{file_path}:{revision}",
                "",
                urllib.request.urlopen(req).read().decode("utf8"),
            )
        except urllib.request.HTTPError:
            if not file_path.startswith("out/"):
                raise

            logging.warning(f"Could not fetch content for {file_path} at revision {revision} from GitHub")
            logging.warning(f"Fetching generated content from googlesource.com may not be accurate")

            # Fall back to googlesource.com if it's a generated file in out/
            req = urllib.request.Request(
                f"https://chromium.googlesource.com/chromium/src/out/+/main/{file_path.removeprefix("out/")}?format=TEXT"
            )

            cached = save_to_cache(
                "raw-content",
                f"{file_path}:{revision}",
                "",
                base64.b64decode(urllib.request.urlopen(req).read().decode("utf8")).decode("utf8"),
            )

    return cached


def llm_minimize_included_header(code: str, model_name: str = "gpt-4.1") -> str:
    model = get_model(model_name=model_name)
    agent = create_agent(model=model)
    prompt = PromptTemplate.from_template(MINIMIZE_CPP_HEADER_TEMPLATE)

    # First minimize using local heuristics before sending to the LLM
    code = minimize_included_header(code)

    result = agent.invoke(
        {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant that is a C++ expert."},
                {
                    "role": "user",
                    "content": prompt.format(content=code),
                },
            ]
        }
    )

    return result["messages"][-1].content.strip()


def replace_include_with_forward_decl(
    include_analysis: IncludeAnalysisOutput,
    source: str,
    target: str,
    model_name: str = "gpt-4.1",
    no_update_cache: bool = False,
) -> Dict[str, any]:
    output_schema = {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Detailed explanation of why the conclusion was reached - point to specific code examples.",
            },
            "forward_declarations": {
                "type": "string",
                "description": "The forward declarations (include namespaces ONLY if they wrap the symbol in the target file) that can be used to replace the include - no comments, extra text, or using declarations. Only include this if `can_replace_include` is true. Remember, symbols might not be in a namespace.",
            },
            "can_replace_include": {
                "type": "boolean",
                "description": "Whether the include directive can be replaced with forward declarations (true) or not (false) - it MUST match the reasoning conclusion",
            },
        },
        "required": ["reasoning", "can_replace_include"],
    }

    revision = include_analysis["revision"]

    raw_source_content = get_file_content(revision, source)
    raw_target_content = get_file_content(revision, target)

    source_content = minimize_includer_header(raw_source_content)

    # Save transformed source content to cache just for debugging purposes
    save_to_cache("source-content", source, raw_source_content, source_content)

    cached_result = load_from_cache(
        "result",
        f"{source}->{target}:{PROMPT_TEMPLATE}:{json.dumps(output_schema)}",
        f"{raw_target_content}\n{source_content}",
    )

    if cached_result:
        return json.loads(cached_result)

    target_content = load_from_cache("content", f"{target}:{MINIMIZE_CPP_HEADER_TEMPLATE}", raw_target_content)

    if not target_content:
        target_content = llm_minimize_included_header(raw_target_content, model_name=model_name)

        # Save off minimized content to cache
        if not no_update_cache:
            save_to_cache("content", f"{target}:{MINIMIZE_CPP_HEADER_TEMPLATE}", raw_target_content, target_content)

    model = get_model(model_name=model_name)
    agent = create_agent(model=model, response_format=output_schema)
    prompt = PromptTemplate.from_template(PROMPT_TEMPLATE)

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "system",
                    "content": "You are a helpful assistant that is a C++ expert with a keen eye for detail.",
                },
                {
                    "role": "user",
                    "content": prompt.format(
                        source_file=source,
                        target_file=target,
                        source_file_content=source_content,
                        target_file_content=target_content,
                    ),
                },
            ]
        }
    )

    if not no_update_cache:
        save_to_cache(
            "result",
            f"{source}->{target}:{PROMPT_TEMPLATE}:{json.dumps(output_schema)}",
            f"{raw_target_content}\n{source_content}",
            json.dumps(result["structured_response"]),
        )

    return result["structured_response"]


def main():
    parser = argparse.ArgumentParser(description="Try to replace an include with a forward declaration.")
    parser.add_argument(
        "include_analysis_output",
        type=str,
        nargs="?",
        help="The include analysis output to use.",
    )
    parser.add_argument("source", help="Source file.")
    parser.add_argument("target", help="Target file.")
    parser.add_argument("--model", help="Name of model to use.")
    parser.add_argument("--no-update-cache", action="store_true", default=False, help="Disable cache updates.")
    parser.add_argument("--verbose", action="store_true", default=False, help="Enable verbose logging.")
    args = parser.parse_args()

    try:
        include_analysis = load_include_analysis(args.include_analysis_output)
    except ParseError as e:
        message = str(e)
        print("error: Could not parse include analysis output file")
        if message:
            print(message)
        return 2

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    if args.source not in include_analysis["files"]:
        print(f"error: {args.source} is not a known file")
        return 1
    elif args.source.endswith(".cc") or args.source.endswith(".c"):
        print(f"error: {args.source} is a source file, not a header file - don't put forward decls in source files")
        return 1

    if args.target not in include_analysis["files"]:
        print(f"error: {args.target} is not a known file")
        return 1

    if args.target not in include_analysis["includes"][args.source]:
        print(f"error: {args.target} is not included by {args.source}")
        return 1

    if "GITHUB_TOKEN" not in os.environ:
        print("error: GITHUB_TOKEN environment variable is not set")
        return 1

    try:
        result = replace_include_with_forward_decl(
            include_analysis,
            args.source,
            args.target,
            model_name=args.model if args.model else None,
            no_update_cache=args.no_update_cache,
        )

        print(json.dumps(result))
        sys.stdout.flush()

        if not result["can_replace_include"]:
            return 1
    except BrokenPipeError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass  # Don't show the user anything
