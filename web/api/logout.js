// Vercel serverless function: /api/logout
import { handlers } from '../lib/runtime.js';

export default function handler(req, res) {
  return handlers().logout(req, res);
}
