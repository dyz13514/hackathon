import { Link } from 'react-router-dom';

export function NotFound() {
  return (
    <section aria-labelledby="notfound-heading">
      <h2 id="notfound-heading">页面不存在</h2>
      <p>该视图尚未落地或路径有误。</p>
      <Link to="/">返回状态看板</Link>
    </section>
  );
}
