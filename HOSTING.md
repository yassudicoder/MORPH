# Hosting TOWER for free

TOWER has two parts:

- **The brain** (`tower/`, Python): one long-running process that holds the live
  simulation in memory. It needs a host that keeps a process running for the
  whole mission.
- **The console** (`console/`, React): plain static files.

Serverless hosts (Vercel, Netlify) and GitHub Pages can't run the brain: their
functions stop after a few minutes and don't share memory. They *can* host the
console, though (see section D). Hugging Face Docker Spaces now need a paid plan.

| You want | Use | Cost | Card needed | LLM |
|---|---|---|---|---|
| A public link for demo day / judges | **A. Your laptop + Cloudflare Tunnel** | ₹0 | no | Ollama on the laptop (full power) |
| A link that stays up for the submission | **B. Render free web service** | ₹0 | no | Groq free key, or rules |
| 24/7 server that also runs Ollama | **C. Oracle Always Free VM** or Azure for Students | ₹0 | Oracle: yes ($1 hold); Azure: no, student check | Ollama on the VM |
| A `vercel.app` address for the console | **D. Vercel (console only)** + the brain on A, B or C | ₹0 | no | whatever the brain uses |

Whichever you choose, **set an access key** so strangers can watch but not take over:

```
TOWER_ACCESS_KEY=pick-a-long-random-word
```

People who open `https://your-url/` get a view-only console. Your team uses
`https://your-url/?key=pick-a-long-random-word`. Everyone sees the same live
mission.

---

## A. Laptop + Cloudflare Tunnel (best for demo day)

This matches the design: the brain runs on the ground-station laptop, with the free local LLM.

1. Install cloudflared once: `winget install --id Cloudflare.cloudflared`
2. Put `TOWER_ACCESS_KEY=...` in `tower/.env`, then start TOWER: `start.bat`
3. In a second terminal: `cloudflared tunnel --url http://localhost:8000`
4. It prints an address like `https://random-words.trycloudflare.com`. Share it, adding `?key=...` for your team.

Limits: the laptop must stay on and awake, the address changes every time you
start the tunnel, and quick tunnels have no uptime guarantee. No account is
needed. **Test the live map through the tunnel once before the event:** the
console needs WebSockets, and venue Wi-Fi sometimes blocks things.

## B. Render free web service (always-available link)

Free plan: 512 MB RAM, 0.1 CPU, no card. TOWER uses about 64 MB and a few
percent of a CPU, so it fits. Ollama does not fit, so use a free Groq key.

1. **Get a free Groq key:** sign in at console.groq.com → API Keys → Create. A
   mission uses 1–3 LLM calls; the free tier allows about 1,000 requests a day
   on `llama-3.3-70b-versatile`.
2. **Put the `tower` folder on GitHub** (a private repo is fine):
   ```bash
   cd tower
   git init
   git add .
   git commit -m "TOWER ground station"
   ```
   Create an empty repo on github.com, then:
   ```bash
   git remote add origin https://github.com/<you>/tower.git
   git branch -M main
   git push -u origin main
   ```
   `.gitignore` already keeps out `node_modules`, the build, `.env` and run logs.
3. **Deploy:** on render.com, sign in with GitHub → **New → Blueprint** → pick the
   repo. Render reads `render.yaml`, asks for `GROQ_API_KEY` (paste it) and
   builds the Dockerfile. The first build takes a few minutes.
4. Open the URL Render gives you (`https://tower-xxxx.onrender.com`). Copy
   `TOWER_ACCESS_KEY` from the service's **Environment** tab and use `?key=...`
   to control the mission.

What to expect on the free plan:
- **Sleeping:** after 15 minutes with no traffic the service sleeps. The next
  visit wakes it in about a minute, and the mission starts fresh. An open
  console tab sends a ping every 4 minutes, so it stays awake while anyone is
  watching. Open it a couple of minutes before you present.
- **Monthly hours:** 750 free instance hours per month (sleeping time doesn't count).
- **Storage:** decision-ledger files are wiped on restart. Download the
  after-action report from the console if you want to keep one.
- **No LLM key?** Set `TOWER_LLM_PROVIDER=rules` instead. The rule-based
  compiler handles the demo mission fine.

## C. Always-on VM with Ollama

**Oracle Cloud Always Free:** an ARM VM (2 OCPU / 12 GB RAM since June 2026)
is enough for Ollama with a 3–4B model. It needs a card for a $1 verification
hold, and free ARM capacity is sometimes unavailable in busy regions.

**Azure for Students:** $100 credit for 12 months, no card, but you must verify
as a student. Stop the VM when you are not using it so the credit lasts.

On the VM (Ubuntu):

```bash
curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen2.5:3b
sudo apt install -y python3-pip nodejs npm git
git clone https://github.com/<you>/tower.git && cd tower
echo "TOWER_ACCESS_KEY=pick-a-long-random-word" > .env
./start.sh
```

Then either open port 8000 in the cloud firewall (Oracle "security list", Azure
"network security group") and visit `http://<vm-ip>:8000`, or run
`cloudflared tunnel --url http://localhost:8000` on the VM to get an https
address without opening ports.

Docker alternative on any VM: `docker build -t tower . && docker run -d -p 8000:8000 --env-file .env tower`.

## D. Console on Vercel, brain somewhere else

Use this if you want the console at a `vercel.app` address. The brain still runs
on Render (B), your laptop with a tunnel (A) or a VM (C). Vercel only serves the
web page, which then connects to the brain.

**Why not the whole thing on Vercel?** Vercel functions on the free (Hobby)
plan stop after at most 5 minutes, and each new connection may land on a
different instance with its own memory. Your mission would vanish mid-run,
and two viewers could see two different missions. Vercel's WebSocket support
is still in beta (since June 2026) and doesn't change those limits.

Steps:

1. Deploy the brain first (A, B or C). You need its **https** address, for
   example `https://tower-xxxx.onrender.com`. A Vercel page is https, and
   browsers block it from connecting to a plain http or `localhost` backend.
2. Push the repo to GitHub (see B, step 2).
3. On vercel.com: **Add New → Project** → import the repo, then set:
   - **Root Directory:** `console`
   - **Framework preset:** Vite (detected automatically: build `npm run build`, output `dist`)
   - **Environment variable:** `VITE_TOWER_BACKEND` = your brain's https address (no trailing slash)
4. Deploy. Open `https://your-project.vercel.app/?key=<TOWER_ACCESS_KEY>` to control the mission.
   The plain link is view-only, exactly as on the brain's own address.

`VITE_TOWER_BACKEND` is baked in at build time. If the brain's address changes
(a new trycloudflare link changes on every restart), update the variable in
Vercel and redeploy. That's why a fixed address like Render's suits this
split best.

The brain's own address keeps serving the console too, so you can use either link.
