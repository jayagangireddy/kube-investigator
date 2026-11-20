"""Create and verify a persistent Foundry prompt agent with the MCP tool."""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from mcp import Client


PROJECT_ENDPOINT = (
    "https://kube-investigator.services.ai.azure.com/api/projects/"
    "jayavardhanazure-8063"
)
MODEL_DEPLOYMENT = "gpt-5-mini"
FOUNDRY_AGENT_NAME = "kube-investigator-mcp-test"
QUESTION = (
    "Use inspect_namespace with namespace paymentservice. Report how many "
    "replicas are ready and whether any containers have restarted."
)


def configured_mcp_url() -> str:
    url = os.environ.get("KUBE_INVESTIGATOR_MCP_URL", "").strip()
    parsed = urlsplit(url)
    if (
        not url
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Set KUBE_INVESTIGATOR_MCP_URL to the full externally reachable "
            "HTTP or HTTPS MCP URL ending in /mcp, without embedded credentials "
            "or query parameters."
        )
    return url


def describe_error(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return "; ".join(describe_error(item) for item in nested)
    return f"{type(exc).__name__}: {exc}"


async def main() -> int:
    try:
        mcp_url = configured_mcp_url()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        # Check the direct, unauthenticated MCP connection before contacting Foundry.
        async with Client(server=mcp_url) as mcp_client:
            discovery = await mcp_client.list_tools()
            discovered = discovery.tools
            print("MCP tools discovered:")
            for tool in discovered:
                print(f"- {tool.name}")

            if not any(tool.name == "inspect_namespace" for tool in discovered):
                print(
                    "Required tool inspect_namespace was not returned by tools/list.",
                    file=sys.stderr,
                )
                return 1

        credential = DefaultAzureCredential()
        try:
            with AIProjectClient(
                endpoint=PROJECT_ENDPOINT,
                credential=credential,
            ) as project:
                # No project_connection_id or headers are configured. Foundry's
                # remote MCP call is therefore unauthenticated, like the preflight.
                mcp_tool = MCPTool(
                    server_label="kube-investigator",
                    server_url=mcp_url,
                    allowed_tools=["inspect_namespace"],
                    require_approval="never",
                )
                agent = project.agents.create_version(
                    agent_name=FOUNDRY_AGENT_NAME,
                    description="Local verification agent for kube-investigator MCP.",
                    definition=PromptAgentDefinition(
                        model=MODEL_DEPLOYMENT,
                        instructions=(
                            "Use inspect_namespace exactly once for the requested namespace. "
                            "Report how many replicas are ready and whether any containers "
                            "have restarted. Report only what the tool output supports."
                        ),
                        tools=[mcp_tool],
                    ),
                )
                print(
                    "Foundry agent created: "
                    f"name={agent.name}, version={agent.version}, id={agent.id}"
                )

                # Confirm the persistent definition can be retrieved from the project.
                stored_agent = project.agents.get(agent_name=FOUNDRY_AGENT_NAME)
                if stored_agent.name != FOUNDRY_AGENT_NAME:
                    print("Could not verify the saved Foundry agent.", file=sys.stderr)
                    return 1
                print(f"Foundry agent verified in project: {stored_agent.name}")

                with project.get_openai_client(agent_name=FOUNDRY_AGENT_NAME) as openai:
                    conversation = openai.conversations.create()
                    try:
                        response = openai.responses.create(
                            conversation=conversation.id,
                            input=QUESTION,
                        )
                    finally:
                        openai.conversations.delete(
                            conversation_id=conversation.id,
                        )

                print("\nAgent answer:")
                print(response.output_text)

                inspect_ran = any(
                    item.type == "mcp_call"
                    and getattr(item, "name", None) == "inspect_namespace"
                    and getattr(item, "status", None) == "completed"
                    for item in response.output
                )
                print(f"inspect_namespace actually ran: {'yes' if inspect_ran else 'no'}")
                print(
                    "Persistent agent remains in Foundry as "
                    f"{FOUNDRY_AGENT_NAME} (version {agent.version})."
                )
                return 0 if inspect_ran else 1
        finally:
            credential.close()
    except Exception as exc:
        # Do not print environment variables, credentials, request headers, or URL.
        detail = describe_error(exc).replace(mcp_url, "<configured MCP URL>")
        response_body = getattr(exc, "body", None)
        if response_body:
            detail += f"; response body: {response_body!r}"
        response = getattr(exc, "response", None)
        response_text = getattr(response, "text", None)
        if response_text:
            detail += f"; HTTP response: {response_text}"
        detail = detail.replace(mcp_url, "<configured MCP URL>")
        print(f"Foundry agent/MCP request failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
