// Vercel serverless function: /api/login
import { handlers } from '../lib/runtime.js';

export default function handler(req, res) {
  return handlers().login(req, res);
}
