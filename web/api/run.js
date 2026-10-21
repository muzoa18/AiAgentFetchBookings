// Vercel serverless function: /api/run
import { handlers } from '../lib/runtime.js';

export default function handler(req, res) {
  return handlers().run(req, res);
}
