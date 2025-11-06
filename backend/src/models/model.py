from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser
from src.models.prompt import summary_prompt



llm = ChatOpenAI(
    model="gpt-4o",
    temperature=0,
    max_tokens=None,
    timeout=None,
    max_retries=2,
    # api_key="...",  # if you prefer to pass api key in directly instaed of using env vars
    # base_url="...",
    # organization="...",
    # other params...
)


def generate_summary(conversation) -> str:
    """
    Takes a conversation string and returns a short summary string.
    """
    chain = summary_prompt | llm | StrOutputParser()
    # Run the chain with the conversation

    summary = chain.invoke({"conversation": conversation})
    
    # Return the summary
    return summary