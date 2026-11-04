import { describe, expect, it } from 'vitest'

import {
  DEFAULT_CHAT_SIZE,
  MIN_CHAT_SIZE,
  dragChatSize,
  fitChatSize,
  maxChatSize,
  readChatSize,
} from './chat-size'

describe('readChatSize', () => {
  it('is the default for nothing, nonsense, and anything that is not two numbers', () => {
    expect(readChatSize(null)).toEqual(DEFAULT_CHAT_SIZE)
    expect(readChatSize('{oops')).toEqual(DEFAULT_CHAT_SIZE)
    expect(readChatSize('{"width":"wide"}')).toEqual(DEFAULT_CHAT_SIZE)
  })

  it('brings a stored size back, no smaller than the minimum', () => {
    expect(readChatSize('{"width":500.4,"height":700}')).toEqual({ width: 500, height: 700 })
    expect(readChatSize('{"width":10,"height":10}')).toEqual(MIN_CHAT_SIZE)
  })
})

describe('fitChatSize', () => {
  it('holds the chosen size inside the viewport and above the minimum', () => {
    expect(fitChatSize({ width: 2000, height: 1200 }, 1000, 800)).toEqual(
      maxChatSize(1000, 800),
    )
    expect(fitChatSize({ width: 100, height: 100 }, 1000, 800)).toEqual(MIN_CHAT_SIZE)
    expect(fitChatSize({ width: 400.6, height: 500.2 }, 1000, 800)).toEqual({
      width: 401,
      height: 500,
    })
  })

  it('never asks for less than the minimum on a tiny window', () => {
    expect(maxChatSize(300, 300)).toEqual(MIN_CHAT_SIZE)
  })
})

describe('dragChatSize', () => {
  const start = { width: 400, height: 500 }

  it('grows leftwards and upwards, because the popup is anchored bottom-right', () => {
    expect(dragChatSize(start, 'corner', -50, -30, 1600, 1000)).toEqual({
      width: 450,
      height: 530,
    })
    expect(dragChatSize(start, 'corner', 50, 30, 1600, 1000)).toEqual({
      width: 350,
      height: 470,
    })
  })

  it('moves one axis on an edge', () => {
    expect(dragChatSize(start, 'left', -50, -30, 1600, 1000)).toEqual({ width: 450, height: 500 })
    expect(dragChatSize(start, 'top', -50, -30, 1600, 1000)).toEqual({ width: 400, height: 530 })
  })

  it('stops at the viewport', () => {
    expect(dragChatSize(start, 'corner', -5000, -5000, 1000, 800)).toEqual(maxChatSize(1000, 800))
  })
})
