"""Runner script for Ngotra AI Platform."""
import os
import sys
import argparse
import uvicorn

def main():
    project_root = os.path.dirname(os.path.abspath(__file__))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    os.chdir(project_root)

    parser = argparse.ArgumentParser(description="Ngotra AI Web App & API Gateway Platform")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8081, help="Port to bind (default: 8081)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()

    print("\n" + "=" * 55)
    print("  🚀 Ngotra AI Platform is running!")
    print(f"  ➜ Landing Page:    http://localhost:{args.port}/")
    print(f"  ➜ User Dashboard:  http://localhost:{args.port}/dashboard")
    print(f"  ➜ Admin Panel:     http://localhost:{args.port}/admin")
    print(f"  ➜ OpenAI Base URL: http://localhost:{args.port}/v1")
    print("=" * 55 + "\n")
    uvicorn.run(
        "platform_app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )

if __name__ == "__main__":
    main()

