"""Stateless chat API backed by Microsoft Agent Framework and the in-cluster MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("kube_investigator_agent")


PROJECT_ENDPOINT = os.getenv(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://kube-investigator.services.ai.azure.com/api/projects/jayavardhanazure-8063",
).strip()
MODEL_DEPLOYMENT = os.getenv("FOUNDRY_MODEL_DEPLOYMENT", "gpt-5-mini").strip()
MCP_URL = os.getenv(
    "KUBE_INVESTIGATOR_MCP_URL",
    "http://kube-investigator-mcp.kube-investigator.svc.cluster.local:8000/mcp",
).strip()
PORT = int(os.getenv("PORT", "8080"))
CHAT_TIMEOUT_SECONDS = float(os.getenv("CHAT_TIMEOUT_SECONDS", "45"))
FOUNDRY_TIMEOUT_SECONDS = float(os.getenv("FOUNDRY_TIMEOUT_SECONDS", "30"))
MCP_TIMEOUT_SECONDS = int(os.getenv("MCP_TIMEOUT_SECONDS", "15"))

INSTRUCTIONS = """You are Kube Investigator, an assistant that answers questions about live Kubernetes workloads.
For any question about whether a namespace or workload is running, call inspect_namespace with the requested namespace before answering. Base the answer only on the live tool result. If the tool reports an error or cannot be reached, clearly say that live inspection failed and do not guess. Explain readiness and restarts from the returned workload and pod data. Do not claim that a workload is healthy when the tool result does not support that conclusion."""


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, Field(max_length=4000)]

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class ChatResponse(BaseModel):
    reply: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    credential = DefaultAzureCredential()
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT,
        credential=credential,
        timeout=FOUNDRY_TIMEOUT_SECONDS,
    )
    app.state.foundry_client = client
    try:
        yield
    finally:
        await client.close()
        await credential.close()


app = FastAPI(title="Kube Investigator Agent API", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    stage = "MCP discovery"
    try:
        async with asyncio.timeout(CHAT_TIMEOUT_SECONDS):
            async with MCPStreamableHTTPTool(
                name="kube-investigator",
                url=MCP_URL,
                allowed_tools=["inspect_namespace"],
                request_timeout=MCP_TIMEOUT_SECONDS,
            ) as mcp_tool:
                stage = "agent orchestration (Foundry model or MCP tool call)"
                agent = Agent(
                    client=app.state.foundry_client,
                    name="kube-investigator-agent",
                    instructions=INSTRUCTIONS,
                    tools=[mcp_tool],
                )
                response = await agent.run(request.message)
        reply = (response.text or "").strip()
        if not reply:
            logger.error("agent_chat_empty_reply")
            raise HTTPException(status_code=502, detail="The agent returned an empty response.")
        return ChatResponse(reply=reply)
    except TimeoutError as exc:
        logger.warning("agent_chat_timeout stage=%s", stage)
        raise HTTPException(
            status_code=504,
            detail=f"The request timed out during {stage}.",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "agent_chat_failed stage=%s error_type=%s",
            stage,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{stage} failed ({type(exc).__name__}). Check agent and MCP service logs.",
        ) from exc
