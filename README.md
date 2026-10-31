# Invoice Action Agent (A2A 1.0)

FastAPI + SQLite A2A invoice agent. Deploy on Render using the included `render.yaml`.

## Deploy quickly
1. Create a new **public GitHub repository** and upload all files in this folder.
2. In Render, choose **New + → Blueprint**, connect the repository, and deploy.
3. In the Render service Environment settings, set:
   - `A2A_BASE_URL` = `https://YOUR-SERVICE.onrender.com/a2a`
   - `A2A_BEARER_TOKEN` = a long private random token (replace generated value with one you retain)
   - `OPENAI_API_KEY` = your API key (optional but strongly recommended for decision accuracy)
   - `OPENAI_MODEL` = `gpt-4o-mini`
4. Save changes and wait for deploy. Free Render services may sleep when idle.
5. Submit **exactly** `https://YOUR-SERVICE.onrender.com/a2a` as the A2A interface base URL.

## Verify
Agent Card: `https://YOUR-SERVICE.onrender.com/.well-known/agent-card.json`
Health: `https://YOUR-SERVICE.onrender.com/`

For protected calls send:
- `Authorization: Bearer YOUR_A2A_BEARER_TOKEN`
- `A2A-Version: 1.0`
- `Content-Type: application/a2a+json`

## Important
The agent proposes actions first. It only records accepted results carrying the grader's receipt nonce; it never invents a payment receipt. Set a persistent disk in Render: SQLite state is stored at `/data/agent.db`.
This is a starter implementation. Test the grader's actual package schema and its continuation envelope before using a limited Check/Save attempt. A model API key materially improves extraction and action accuracy; without it the conservative fallback is intentionally less capable.
