import { Routes, Route } from 'react-router-dom'
import { Layout } from './components/Layout.jsx'
import { Dashboard } from './routes/Dashboard.jsx'
import { Auth } from './routes/Auth.jsx'
import { Onboarding } from './routes/Onboarding.jsx'
import { Coach } from './routes/Coach.jsx'
import { Decisions } from './routes/Decisions.jsx'
import { Settings } from './routes/Settings.jsx'

/**
 * Exactly six surface routes for Ballast v1. No guru / curriculum / quiz.
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/auth" element={<Auth />} />
        <Route path="/onboarding" element={<Onboarding />} />
        <Route path="/coach" element={<Coach />} />
        <Route path="/decisions" element={<Decisions />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}
