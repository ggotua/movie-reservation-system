# Frontend — Movie Reservation System user app

React + TypeScript (strict) + Vite, Tailwind CSS + shadcn/ui, Vitest + Testing
Library + MSW. The user-facing screens are added by later prompts; this is the
project skeleton.

## Requirements

- Node.js 24 (see `.nvmrc`) and npm.

## Install and run

```bash
npm install
npm run dev
```

The dev server runs at http://localhost:5173 and calls the backend at
`VITE_API_BASE_URL` (default `http://127.0.0.1:8000`).

## Environment

Copy `.env.example` to `.env` and adjust if needed. Every `VITE_*` variable is
public in the bundle, so never put a secret in one.

| Variable               | Default                 | Purpose                              |
| ---------------------- | ----------------------- | ------------------------------------ |
| `VITE_API_BASE_URL`    | `http://127.0.0.1:8000` | Backend API base URL                 |
| `VITE_CURRENCY`        | `GEL`                   | Currency used to format money        |
| `VITE_CINEMA_TIMEZONE` | `Asia/Tbilisi`          | Time zone all showtimes are shown in |

Only `src/lib/config.ts` reads these.

## Scripts

| Script              | Command                                | Purpose                         |
| ------------------- | -------------------------------------- | ------------------------------- |
| `npm run dev`       | `vite`                                 | Development server              |
| `npm run build`     | `tsc -b && vite build`                 | Type check and production build |
| `npm run lint`      | `eslint .`                             | Lint                            |
| `npm run typecheck` | `tsc --noEmit` (app and node projects) | Strict type check               |
| `npm test`          | `vitest run`                           | Unit and component tests        |
| `npm run format`    | `prettier --write .`                   | Format the project              |
| `npm run preview`   | `vite preview`                         | Serve the production build      |
