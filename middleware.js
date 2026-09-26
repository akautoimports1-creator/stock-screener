// Password-protects the whole site using Vercel's free Edge Middleware +
// HTTP Basic Auth (the browser's native login popup) — no paid Vercel plan
// needed. The actual username/password are NOT in this file (this repo is
// public) — they're set as Environment Variables in the Vercel dashboard
// (Project Settings -> Environment Variables -> SCREENER_USER / SCREENER_PASS),
// read here at request time via process.env.
//
// If those env vars aren't set yet, this deliberately lets requests through
// rather than locking everyone out by accident.

export const config = {
  matcher: '/:path*',
};

export default async function middleware(request) {
  const user = process.env.SCREENER_USER;
  const pass = process.env.SCREENER_PASS;

  if (!user || !pass) {
    return; // not configured yet — don't block access
  }

  const authHeader = request.headers.get('authorization');
  if (authHeader && authHeader.startsWith('Basic ')) {
    try {
      const decoded = atob(authHeader.slice(6));
      const sep = decoded.indexOf(':');
      const suppliedUser = decoded.slice(0, sep);
      const suppliedPass = decoded.slice(sep + 1);
      if (suppliedUser === user && suppliedPass === pass) {
        return; // correct credentials — let the request through
      }
    } catch (e) {
      // fall through to the 401 below
    }
  }

  return new Response('Authentication required', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="Ratio Exchange"',
    },
  });
}
