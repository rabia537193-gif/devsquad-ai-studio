import os
from openai import OpenAI

def run_developer(plan: str) -> str:
    client = OpenAI(
        api_key=os.getenv("GROQ_API_KEY"),
        base_url="https://api.groq.com/openai/v1"
    )
    prompt = f"""
    You are a Senior Backend Python Developer. 
    Based on the implementation plan below, generate modular, clean, and production-ready 
    Python code with robust error handling and docstrings.
    Return ONLY valid Python code inside a markdown code block.

    Implementation Plan:
    {plan}
    """
    
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    content = response.choices[0].message.content
    if "```python" in content:
        content = content.split("```python")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].split("```")[0].strip()
    return content