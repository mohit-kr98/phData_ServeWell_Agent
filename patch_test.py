import sys
import types
module = types.ModuleType('langchain_community.chat_models.vertexai')
sys.modules['langchain_community.chat_models.vertexai'] = module
module.ChatVertexAI = None

try:
    import ragas
    print("Patch worked! ragas imported.")
except Exception as e:
    print(f"Error: {e}")
