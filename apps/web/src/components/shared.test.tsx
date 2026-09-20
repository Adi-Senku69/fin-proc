import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ApiBoundary } from './shared'
import type { ApiState } from '../api/useApiState'

describe('ApiBoundary', () => {
  it('renders a loading panel while loading', () => {
    render(
      <ApiBoundary state={{ status: 'loading' } satisfies ApiState<string>} loadingText="Loading X...">
        {() => <div>should not render</div>}
      </ApiBoundary>,
    )
    expect(screen.getByText('Loading X...')).toBeInTheDocument()
  })

  it('renders a specific error panel on failure, never a blank one', () => {
    render(
      <ApiBoundary state={{ status: 'error', httpStatus: 500, message: 'kaboom' } satisfies ApiState<string>}>
        {() => <div>should not render</div>}
      </ApiBoundary>,
    )
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument()
    expect(screen.getByText('kaboom')).toBeInTheDocument()
  })

  it('renders a distinct "not available" panel for a 404', () => {
    render(
      <ApiBoundary state={{ status: 'error', httpStatus: 404, message: 'not found' } satisfies ApiState<string>}>
        {() => <div>should not render</div>}
      </ApiBoundary>,
    )
    expect(screen.getByText(/not available yet/)).toBeInTheDocument()
  })

  it('renders the children with the data once ready', () => {
    render(
      <ApiBoundary state={{ status: 'ready', data: 'hello' } satisfies ApiState<string>}>
        {(data) => <div>got: {data}</div>}
      </ApiBoundary>,
    )
    expect(screen.getByText('got: hello')).toBeInTheDocument()
  })
})
