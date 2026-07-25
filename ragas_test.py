import os
from dotenv import load_dotenv
load_dotenv()

api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
base_url = "https://openrouter.ai/api/v1" if (not os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENROUTER_API_KEY")) else None

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
llm = ChatOpenAI(
    model="openai/gpt-4o",
    api_key=api_key or "dummy",
    base_url=base_url
)
embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=api_key or "dummy",
    base_url=base_url
)
from ragas.testset.generator import TestsetGenerator
from ragas.testset.evolutions import simple, reasoning, multi_context

generator = TestsetGenerator.from_langchain(
    generator_llm=llm,
    critic_llm=llm,
    embeddings=embeddings
)
print("Ragas imported and generator instantiated successfully!")
