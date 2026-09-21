import { Link } from 'react-router-dom';

export function NotFound() {
  return (
    <section aria-labelledby="notfound-heading">
      <h2 id="notfound-heading">Page not found</h2>
      <p>This view has not been implemented yet, or the path is incorrect.</p>
      <Link to="/">Back to Dashboard</Link>
    </section>
  );
}
