#include <cstdint>
#include <iostream>
#include <ostream>
#define foo(x, y) ((x > y) ? x : y)

int main()
{	
    int a = 0;
    int b = 0;
    int c = foo(a++, b++);
    std::cout << a << b << c << std::endl;
}
