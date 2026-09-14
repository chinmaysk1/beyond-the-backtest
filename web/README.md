# web — Next.js application

Not yet built. Lands in Weeks 9–11.

Talks to Postgres directly for reads (bars, universe, results) and enqueues
work by inserting into `jobs`. It never calls the Python backend synchronously;
see `../backend/btb/schema.sql` for the `jobs` table that is the seam between
them.
