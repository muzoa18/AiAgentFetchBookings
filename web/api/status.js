// Vercel serverless function: /api/status
import { handlers } from '../lib/runtime.js';

export default function handler(req, res) {
  return handlers().status(req, res);
}
