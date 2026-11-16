import os
from openai import OpenAI

def run_planner(requirement: str) -> str:
    client = OpenAI(
        api_key=os.getenv("GROQ_API_KEY"),
        base_url="https://api.groq.com/openai/v1"
    )
    prompt = f"""
    You are a Software Architect and Project Planner. 
    Analyze the following software requirement and create a detailed implementation plan, 
    specifying functions, classes, data structures, and the overall technical design.

    Requirement:
    {requirement}
    """
    
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    return response.choices[0].message.content