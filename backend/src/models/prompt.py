from langchain_core.prompts import ChatPromptTemplate 



summary_prompt = ChatPromptTemplate.from_template("""
You are an AI assistant. 
Here is the conversation between user and AI:

{conversation}

Generate a short, clear summary in 2-3 sentences.
""")
