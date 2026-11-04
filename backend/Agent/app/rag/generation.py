"""Generation agent: answer from the assembled context through LiteLLM (Gemini)."""
from .. import llm


def generate(question: str, context: str) -> str:
    if not context.strip():
        return "I could not find anything about that in the processed invoices."
    return llm.complete(llm.prompt_messages("rag_generation", question=question, context=context),
                        max_tokens=800).strip()
