import { Routes, Route } from 'react-router-dom'
import { Layout } from './components/Layout.jsx'
import { Dashboard } from './routes/Dashboard.jsx'
import { Auth } from './routes/Auth.jsx'
import { Onboarding } from './routes/Onboarding.jsx'
import { BrokerageCallback } from './routes/BrokerageCallback.jsx'
import { Coach } from './routes/Coach.jsx'
import { Decisions } from './routes/Decisions.jsx'
import { Settings } from './routes/Settings.jsx'

/**
 * Exactly six surface routes for Ballast v1. No guru / curriculum / quiz.
 * (`/callback` is not a surface — it's the transient landing page for the real
 * Schwab OAuth redirect, which completes the link in-app and moves the user on.)
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/auth" element={<Auth />} />
        <Route path="/onboarding" element={<Onboarding />} />
        <Route path="/callback" element={<BrokerageCallback />} />
        <Route path="/coach" element={<Coach />} />
        <Route path="/decisions" element={<Decisions />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}
