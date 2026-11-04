import { describe, expect, it } from 'vitest'

import { imageLabel, isImageFile, isRemoteImage } from './images'

describe('isImageFile', () => {
  it('takes a dropped picture by its suffix, whatever the browser calls its type', () => {
    for (const name of ['shot.png', 'SHOT.PNG', 'a photo.JPEG', 'icon.svg', 'sheet.webp']) {
      expect(isImageFile({ name }), name).toBe(true)
    }
  })

  it('refuses everything the backend would refuse', () => {
    // The suffix decides on both sides — `explorer/service.py`'s IMAGE_SUFFIXES — so a file
    // the browser labels `image/avif` is not accepted here either.
    for (const name of ['notes.txt', 'clip.mp4', 'photo.avif', 'photo.heic', 'screenshot']) {
      expect(isImageFile({ name }), name).toBe(false)
    }
  })
})

describe('isRemoteImage', () => {
  it('accepts every shape a document beside its pictures uses', () => {
    for (const src of [
      'shot.png',
      './shot.png',
      '../assets/shot.png',
      '/shared/shot.png',
      'a%20shot.png',
      'ChatGPT_Image_Jun_29_2025(1).png',
    ]) {
      expect(isRemoteImage(src), src).toBe(false)
    }
  })

  it('refuses anything naming a host or a machine', () => {
    // The same rule the backend applies (`urlsplit(...).scheme or startswith("//")`), so the
    // editor never asks for a picture `DocStore.asset` was going to refuse.
    for (const src of [
      'https://example.invalid/i.png',
      'HTTP://example.invalid/i.png',
      'data:image/png;base64,AAAA',
      '//example.invalid/i.png',
      'C:/art/hero.png',
    ]) {
      expect(isRemoteImage(src), src).toBe(true)
    }
  })
})

describe('imageLabel', () => {
  it('prefers the alt text the author wrote', () => {
    expect(imageLabel('shot.png', 'The medical bay')).toBe('The medical bay')
  })

  it('falls back to the file name, decoded', () => {
    expect(imageLabel('images/a%20shot.png')).toBe('a shot.png')
    expect(imageLabel('shot.png', '   ')).toBe('shot.png')
  })

  it('shows a reference it cannot decode as written', () => {
    // `%zz` is not encoding. A label may be ugly; it may not be empty or thrown away.
    expect(imageLabel('a%zz.png')).toBe('a%zz.png')
  })
})
