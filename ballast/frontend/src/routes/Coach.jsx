import { RecoveryPrecedent } from '../components/RecoveryPrecedent.jsx'
import './screen.css'

export function Coach() {
  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">coach</p>
      <h1 className="ballast-screen__title">Coach</h1>
      <p className="ballast-screen__prose">
        A calm, plain-spoken guide. Ask a question and think out loud — no
        jargon, no pressure.
      </p>
      <RecoveryPrecedent />
    </section>
  )
}
