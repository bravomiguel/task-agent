from typing import Literal  
from tavily import TavilyClient  
import os  
from langchain.agents import create_agent  
from langchain.chat_models import init_chat_model  
from deepagents.middleware import PlanningMiddleware, SubAgentMiddleware  
  
# Initialize Tavily and define search tool  
tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])  
  
def internet_search(  
    query: str,  
    max_results: int = 5,  
    topic: Literal["general", "news", "finance"] = "general",  
    include_raw_content: bool = False,  
):  
    """Run a web search"""  
    return tavily_client.search(  
        query,  
        max_results=max_results,  
        include_raw_content=include_raw_content,  
        topic=topic,  
    )  
  
# Create the GPT-5 model instance  
model = init_chat_model(model="openai:gpt-4.1")  
  
# Define your tools list  
your_tools = [internet_search]  
  
# Define your subagents  
subagents = [  
    {  
        "name": "research-agent",  
        "description": "Used to research in-depth questions",  
        "prompt": "You are an expert researcher.",  
        "tools": [internet_search]  
    }  
]  
  
# Create middleware stack  
middleware = [  
    PlanningMiddleware(),  
    SubAgentMiddleware(  
        default_subagent_tools=your_tools,  
        subagents=subagents,  
        model=model,  
        is_async=False  
    ),  
]  
  
agent = create_agent(  
    model=model,  
    middleware=middleware,  
    tools=your_tools,  
    prompt="You are a todo list actioning agent. When responding directly to the user, make sure your message is in markdown."  
)
