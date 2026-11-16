import os
import sys
from dotenv import load_dotenv
from agents import run_planner, run_developer, run_qa, run_reviewer

load_dotenv()

def main():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("❌ Error: GROQ_API_KEY nahi mili!")
        print("👉 Apne project folder mein '.env' file banayein aur usmein yeh likhein:")
        print("   GROQ_API_KEY=gsk_aapki_free_groq_key_yahan")
        sys.exit(1)

    print("🤖 AI Software Development Team Initialized...\n")
    
    requirement = input("Enter your software requirement: ").strip()
    if not requirement:
        requirement = "Build a robust SQLite-backed Task Management API helper in Python with add, update, delete, and list tasks functionality."
    
    os.makedirs("output", exist_ok=True)
    
    print("\n[1/4] 📐 Project Planner is analyzing requirements & designing architecture...")
    plan = run_planner(requirement)
    with open("output/implementation_plan.md", "w", encoding="utf-8") as f:
        f.write(plan)
    print("   -> Saved: output/implementation_plan.md")
    
    print("\n[2/4] 💻 Python Developer is writing modular implementation code...")
    code = run_developer(plan)
    with open("output/app.py", "w", encoding="utf-8") as f:
        f.write(code)
    print("   -> Saved: output/app.py")
    
    print("\n[3/4] 🧪 QA Engineer is creating comprehensive unit tests and edge cases...")
    test_code = run_qa(code)
    with open("output/test_app.py", "w", encoding="utf-8") as f:
        f.write(test_code)
    print("   -> Saved: output/test_app.py")
    
    print("\n[4/4] 🔍 Code Reviewer is performing code review & generating final report...")
    review_report = run_reviewer(code, test_code)
    with open("output/development_report.md", "w", encoding="utf-8") as f:
        f.write(review_report)
    print("   -> Saved: output/development_report.md")
    
    print("\n✨ Workflow Complete! All files saved inside the 'output/' folder.")

if __name__ == "__main__":
    main()