package cache

import "errors"

// ErrMissing is returned when a key is absent.
var ErrMissing = errors.New("missing key")

// Store keeps values in memory.
type Store struct {
	values map[string]string
}

// NewStore builds an empty Store.
func NewStore() *Store {
	return &Store{values: make(map[string]string)}
}

// Put writes a value.
func (s *Store) Put(key, value string) {
	s.values[key] = value
}

// Get reads a value, or ErrMissing.
func (s *Store) Get(key string) (string, error) {
	value, ok := s.values[key]
	if !ok {
		return "", ErrMissing
	}
	return value, nil
}

// Warm fills a store with one entry and reads it back.
func Warm() (string, error) {
	store := NewStore()
	store.Put("greeting", "hello")
	return store.Get("greeting")
}
