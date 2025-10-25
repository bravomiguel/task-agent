from tavily import TavilyClient
import os
from typing import Literal
from langchain.chat_models import init_chat_model
from agent.middleware import ReviewMessageMiddleware, ThreadTitleMiddleware, IsDoneMiddleware
from agent.create_deep_agent import create_deep_agent

gpt_5 = init_chat_model(model="openai:gpt-5", reasoning={"effort": "minimal"})
gpt_4_1 = init_chat_model(model="openai:gpt-4.1")
gpt_5_mini = init_chat_model(model="openai:gpt-5-mini", disable_streaming=True)

# It's best practice to initialize the client once and reuse it.
tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

# Search tool to use to do research


def internet_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
):
    """Run a web search"""
    search_docs = tavily_client.search(
        query,
        max_results=max_results,
        include_raw_content=include_raw_content,
        topic=topic,
    )
    return search_docs


agent = create_deep_agent(
    system_prompt="You are a general task actioning agent. Always respond to the user in markdown. After the first user message, always use write_todos to plan out how you will approach the task initially. You can of course update this plan over time as you make progress and find out new context etc.",
    model=gpt_4_1,
    middleware=[IsDoneMiddleware(), ThreadTitleMiddleware(
        llm=gpt_5_mini), ReviewMessageMiddleware(llm=gpt_5_mini)],
    tools=[internet_search],
)
