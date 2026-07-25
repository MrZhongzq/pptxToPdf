import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { UploadDropzone } from './UploadDropzone'

const MAX = 600 * 1024 * 1024

function fileOfSize(size: number): File {
  const f = new File(['x'], 'deck.pptx')
  Object.defineProperty(f, 'size', { value: size })
  return f
}

describe('UploadDropzone', () => {
  it('accepts a valid pptx', () => {
    const onFileSelected = vi.fn()
    render(<UploadDropzone onFileSelected={onFileSelected} maxBytes={MAX} />)

    const input = screen.getByTestId('file-input')
    fireEvent.change(input, { target: { files: [fileOfSize(1024)] } })

    expect(onFileSelected).toHaveBeenCalledOnce()
  })

  it('rejects a file over the limit and shows a message', () => {
    const onFileSelected = vi.fn()
    render(<UploadDropzone onFileSelected={onFileSelected} maxBytes={MAX} />)

    const input = screen.getByTestId('file-input')
    fireEvent.change(input, { target: { files: [fileOfSize(MAX + 1)] } })

    expect(onFileSelected).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('超过上限')
  })

  it('rejects a non-pptx extension', () => {
    const onFileSelected = vi.fn()
    render(<UploadDropzone onFileSelected={onFileSelected} maxBytes={MAX} />)

    const wrong = new File(['x'], 'notes.pdf')
    fireEvent.change(screen.getByTestId('file-input'), {
      target: { files: [wrong] },
    })

    expect(onFileSelected).not.toHaveBeenCalled()
    expect(screen.getByRole('alert')).toHaveTextContent('.pptx')
  })
})
