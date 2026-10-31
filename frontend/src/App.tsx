import { Button } from '@/components/ui/button'

/**
 * Application root. A placeholder screen for now: it renders the display
 * typeface and a primary button so the tokens are visible. Real screens arrive
 * with their features (SPEC-7 sections 2.3-2.5).
 */
function App() {
  return (
    <main className="mx-auto flex min-h-svh w-full max-w-5xl flex-col items-start justify-center gap-6 px-6">
      <h1 className="text-7">Showtimes</h1>
      <Button className="h-10 px-4">Hold seats</Button>
    </main>
  )
}

export default App
