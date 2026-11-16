import os
from openai import OpenAI

def run_reviewer(code: str, test_code: str) -> str:
    client = OpenAI(
        api_key=os.getenv("GROQ_API_KEY"),
        base_url="https://api.groq.com/openai/v1"
    )
    prompt = f"""
    You are a Senior Code Reviewer. Review the Python implementation and its corresponding test suite.
    Identify potential bugs, security flaws, performance bottlenecks, code smells, and recommend concrete improvements.
    Provide a professional Code Review and Quality Report.

    --- IMPLEMENTATION CODE ---
    {code}

    --- TEST CODE ---
    {test_code}
    """
    
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    return response.choices[0].message.content