import os
from openai import OpenAI

def run_qa(code: str) -> str:
    client = OpenAI(
        api_key=os.getenv("GROQ_API_KEY"),
        base_url="https://api.groq.com/openai/v1"
    )
    prompt = f"""
    You are an expert QA Engineer and Software Tester. 
    Based on the Python code provided below, generate comprehensive unit tests using Python's `unittest` or `pytest` framework. 
    Include edge cases, error cases, and invalid inputs.
    Return ONLY valid Python test code inside a markdown code block.

    Source Code:
    {code}
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